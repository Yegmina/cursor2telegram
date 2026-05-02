"""Small admin CLI: validate config, send a test message, etc.

Usage:
    c2t-admin check
    c2t-admin send-test "hello from c2t"
    c2t-admin print-config
    c2t-admin getme
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict

from .config import load_config
from .logging_setup import configure_logging, get_logger


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="c2t-admin")
    p.add_argument("--config", "-c", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check")
    sub.add_parser("print-config")
    sub.add_parser("getme")
    s_send = sub.add_parser("send-test")
    s_send.add_argument("text")
    s_send.add_argument("--chat-id", type=int, default=None)
    return p.parse_args(argv)


async def _getme(token: str) -> dict:
    from telegram import Bot

    async with Bot(token) as bot:
        me = await bot.get_me()
        return me.to_dict()


async def _send(token: str, chat_id: int, text: str) -> dict:
    from telegram import Bot

    async with Bot(token) as bot:
        msg = await bot.send_message(chat_id=chat_id, text=text)
        return msg.to_dict()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    log = get_logger("c2t-admin")
    cfg = load_config(args.config)

    if args.cmd == "check":
        problems = []
        if not cfg.telegram.bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set")
        if not cfg.telegram.allowed_user_ids and not cfg.telegram.allowed_chat_ids:
            problems.append("no allowed_user_ids/allowed_chat_ids configured")
        if problems:
            for p in problems:
                log.error("check.fail", problem=p)
            return 1
        log.info("check.ok")
        return 0

    if args.cmd == "print-config":
        print(json.dumps(asdict(cfg), default=str, indent=2))
        return 0

    if args.cmd == "getme":
        if not cfg.telegram.bot_token:
            log.error("missing bot token")
            return 2
        info = asyncio.run(_getme(cfg.telegram.bot_token))
        print(json.dumps(info, indent=2))
        return 0

    if args.cmd == "send-test":
        chat_id = args.chat_id or (
            cfg.telegram.owner_user_id or (cfg.telegram.allowed_user_ids[0] if cfg.telegram.allowed_user_ids else None)
        )
        if not chat_id:
            log.error("no chat id available")
            return 2
        info = asyncio.run(_send(cfg.telegram.bot_token, chat_id, args.text))
        print(json.dumps(info, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
