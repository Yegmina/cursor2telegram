"""Process entry point for cursor2telegram."""

from __future__ import annotations

import argparse
import os
import sys

from .bot import BotApp
from .config import load_config
from .logging_setup import configure_logging, get_logger


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="cursor2telegram", description="Telegram front-end for Cursor CLI.")
    p.add_argument(
        "--config",
        "-c",
        default=os.environ.get("CURSOR2TELEGRAM_CONFIG"),
        help="Path to TOML config file. Default: /etc/cursor2telegram/config.toml",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("CURSOR2TELEGRAM_LOG_LEVEL", "INFO"),
        help="Log level (DEBUG, INFO, WARNING, ERROR). Default: INFO",
    )
    p.add_argument(
        "--log-json",
        action="store_true",
        default=None,
        help="Force structured JSON logs (default: JSON when not on a TTY).",
    )
    p.add_argument("--print-config", action="store_true", help="Print resolved config and exit.")
    p.add_argument(
        "--check",
        action="store_true",
        help="Validate environment (token, agent binary) and exit non-zero on failure.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(level=args.log_level, json=args.log_json)
    log = get_logger("cursor2telegram")

    cfg = load_config(args.config)
    log.info(
        "config.loaded",
        path=str(cfg.config_path) if cfg.config_path else None,
        allowed_users=len(cfg.telegram.allowed_user_ids),
        agent_binary=cfg.resolved_agent_binary(),
    )

    if args.print_config:
        import json as _json
        from dataclasses import asdict

        print(_json.dumps(asdict(cfg), default=str, indent=2))
        return 0

    if args.check:
        problems: list[str] = []
        if not cfg.telegram.bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is not set")
        if not cfg.telegram.allowed_user_ids and not cfg.telegram.allowed_chat_ids:
            problems.append("no allowed_user_ids/allowed_chat_ids configured")
        agent_bin = cfg.resolved_agent_binary()
        if agent_bin == "agent" and not _which(agent_bin):
            problems.append(f"agent binary not found on PATH ({agent_bin})")
        if problems:
            for p in problems:
                log.error("check.fail", problem=p)
            return 1
        log.info("check.ok")
        return 0

    app = BotApp(cfg)
    try:
        app.run()
        return 0
    except KeyboardInterrupt:
        log.info("shutdown.signal")
        return 0


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


if __name__ == "__main__":
    sys.exit(main())
