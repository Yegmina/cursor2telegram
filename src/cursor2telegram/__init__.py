"""cursor2telegram — Telegram bot front-end for Cursor CLI (ACP).

Public API:
    main()              process entry point
    Config              immutable configuration object
    BotApp              high-level orchestrator
    AcpClient           low-level ACP JSON-RPC client over stdio
    SessionManager      per-chat session bookkeeping
    Policy              access and confirmation policy
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "AcpClient",
    "BotApp",
    "Config",
    "Policy",
    "SessionManager",
    "__version__",
    "main",
]


def __getattr__(name: str):
    if name == "main":
        from .__main__ import main as _main

        return _main
    if name == "Config":
        from .config import Config as _Config

        return _Config
    if name == "BotApp":
        from .bot import BotApp as _BotApp

        return _BotApp
    if name == "AcpClient":
        from .acp import AcpClient as _AcpClient

        return _AcpClient
    if name == "SessionManager":
        from .sessions import SessionManager as _SessionManager

        return _SessionManager
    if name == "Policy":
        from .policy import Policy as _Policy

        return _Policy
    raise AttributeError(name)
