"""Telegram UI helpers: message chunking, escaping, inline keyboards."""

from __future__ import annotations

import html
import io
import json
from collections.abc import Iterable
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

TELEGRAM_MAX_CHARS = 4096


def html_escape(text: str) -> str:
    return html.escape(text, quote=False)


def code_block(text: str, language: str = "") -> str:
    safe = html.escape(text, quote=False)
    if language:
        return f'<pre><code class="language-{html_escape(language)}">{safe}</code></pre>'
    return f"<pre>{safe}</pre>"


def split_for_telegram(text: str, max_chars: int = TELEGRAM_MAX_CHARS - 16) -> list[str]:
    """Split text into Telegram-sized chunks at line/word boundaries."""
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n ")
    if remaining:
        parts.append(remaining)
    return parts


def as_document(name: str, content: str) -> tuple[str, io.BytesIO]:
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = name
    return name, bio


def kb_modes(active: str = "agent") -> InlineKeyboardMarkup:
    rows = []
    row = []
    for m in ("agent", "plan", "ask"):
        marker = "● " if m == active else ""
        row.append(InlineKeyboardButton(f"{marker}{m}", callback_data=f"mode:{m}"))
    rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_model_menu(
    models: list[dict[str, Any]],
    *,
    active_model_id: str = "",
    token_map: dict[str, str] | None = None,
    limit: int = 12,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    token_map = token_map or {}
    for i, model in enumerate(models[:limit]):
        model_id = str(model.get("modelId") or model.get("id") or model.get("value") or "")
        if not model_id:
            continue
        token = next((k for k, v in token_map.items() if v == model_id), f"m{i}")
        name = str(model.get("name") or model_id)
        marker = "● " if model_id == active_model_id else ""
        rows.append([InlineKeyboardButton(f"{marker}{name}", callback_data=f"model:set:{token}")])
    rows.append(
        [
            InlineKeyboardButton("Auto", callback_data="model:set:auto"),
            InlineKeyboardButton("Refresh", callback_data="cmd:model"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def kb_permission(tool_call_id: str | None) -> InlineKeyboardMarkup:
    payload = tool_call_id or "_"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Allow once", callback_data=f"perm:once:{payload}"),
                InlineKeyboardButton("Allow always", callback_data=f"perm:always:{payload}"),
            ],
            [InlineKeyboardButton("Reject", callback_data=f"perm:reject:{payload}")],
        ]
    )


def kb_plan(tool_call_id: str | None) -> InlineKeyboardMarkup:
    payload = tool_call_id or "_"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Accept plan", callback_data=f"plan:accept:{payload}"),
                InlineKeyboardButton("Reject", callback_data=f"plan:reject:{payload}"),
            ],
        ]
    )


def kb_question(
    tool_call_id: str | None,
    question_id: str,
    options: Iterable[dict[str, Any]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for opt in options:
        oid = str(opt.get("id", ""))
        label = str(opt.get("label", oid))
        cb = f"q:{tool_call_id or '_'}:{question_id}:{oid}"
        row.append(InlineKeyboardButton(label, callback_data=cb[:64]))
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Skip", callback_data=f"q:{tool_call_id or '_'}:{question_id}:__skip")])
    return InlineKeyboardMarkup(rows)


def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Status", callback_data="cmd:status"),
                InlineKeyboardButton("New session", callback_data="cmd:new"),
                InlineKeyboardButton("Cancel", callback_data="cmd:cancel"),
            ],
            [
                InlineKeyboardButton("Mode", callback_data="cmd:mode"),
                InlineKeyboardButton("Model", callback_data="cmd:model"),
                InlineKeyboardButton("Workspace", callback_data="cmd:workspace"),
            ],
            [
                InlineKeyboardButton("Files", callback_data="cmd:files"),
                InlineKeyboardButton("MCP", callback_data="cmd:mcp"),
                InlineKeyboardButton("Help", callback_data="cmd:help"),
            ],
        ]
    )


def kb_yes_no(prefix: str, payload: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data=f"{prefix}:yes:{payload}"),
                InlineKeyboardButton("No", callback_data=f"{prefix}:no:{payload}"),
            ]
        ]
    )


def render_tool_call(name: str, args: dict[str, Any] | None, *, max_arg_chars: int = 600) -> str:
    args_str = ""
    if args:
        try:
            args_str = json.dumps(args, ensure_ascii=False, indent=2, sort_keys=True)
        except (TypeError, ValueError):
            args_str = repr(args)
        if len(args_str) > max_arg_chars:
            args_str = args_str[: max_arg_chars - 1] + "…"
    body = f"<b>tool</b> <code>{html_escape(name)}</code>"
    if args_str:
        body += "\n" + code_block(args_str, "json")
    return body


def render_plan(payload: dict[str, Any]) -> str:
    name = payload.get("name") or "Plan"
    overview = payload.get("overview") or ""
    plan = payload.get("plan") or ""
    todos = payload.get("todos") or []
    parts = [f"<b>{html_escape(str(name))}</b>"]
    if overview:
        parts.append(html_escape(str(overview)))
    if plan:
        parts.append(code_block(str(plan), "markdown"))
    if todos:
        bullets = []
        for t in todos:
            status = str(t.get("status", "pending"))
            mark = {
                "completed": "[x]",
                "in_progress": "[~]",
                "cancelled": "[-]",
                "pending": "[ ]",
            }.get(status, "[ ]")
            bullets.append(f"{mark} {html_escape(str(t.get('content', '')))}")
        parts.append("<b>Todos</b>\n" + "\n".join(bullets))
    return "\n\n".join(parts)


def render_question(payload: dict[str, Any]) -> str:
    title = payload.get("title") or "Question"
    questions = payload.get("questions") or []
    parts = [f"<b>{html_escape(str(title))}</b>"]
    for q in questions:
        prompt = q.get("prompt") or ""
        parts.append(html_escape(str(prompt)))
    return "\n\n".join(parts)


PARSE_MODE = ParseMode.HTML
