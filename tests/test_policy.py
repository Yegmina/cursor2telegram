from cursor2telegram.config import (
    Config,
    CursorConfig,
    PolicyConfig,
    StorageConfig,
    TelegramConfig,
    UxConfig,
)
from cursor2telegram.policy import Policy


def make_cfg(**tg_overrides):
    return Config(
        telegram=TelegramConfig(**tg_overrides),
        cursor=CursorConfig(workspace="/tmp/ws"),
        policy=PolicyConfig(),
        ux=UxConfig(),
        storage=StorageConfig(),
    )


def test_policy_denies_without_allowlist():
    p = Policy(make_cfg())
    assert not p.check_user(1, 2).allowed


def test_policy_allows_user_in_allowlist():
    p = Policy(make_cfg(allowed_user_ids=(7,)))
    assert p.check_user(7, None).allowed
    assert not p.check_user(8, None).allowed


def test_policy_allows_chat_in_allowlist():
    p = Policy(make_cfg(allowed_chat_ids=(99,)))
    assert p.check_user(None, 99).allowed


def test_categorize_tool():
    p = Policy(make_cfg(allowed_user_ids=(1,)))
    assert p.categorize_tool("ShellExec") == "shell"
    assert p.categorize_tool("WriteFile") == "write"
    assert p.categorize_tool("DeleteFile") == "delete"
    assert p.categorize_tool("WebFetch") == "network"
    assert p.categorize_tool("ReadFile") == "read"
    assert p.categorize_tool("call_mcp_tool") == "mcp"


def test_workspace_contains():
    p = Policy(make_cfg(allowed_user_ids=(1,)))
    assert p.workspace_contains("/tmp/ws/foo")
    assert not p.workspace_contains("/etc/passwd")


def test_owner_default_is_first_allowed():
    p = Policy(make_cfg(allowed_user_ids=(7, 8)))
    assert p.is_owner(7)
    assert not p.is_owner(8)
