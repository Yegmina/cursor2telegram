"""Session / workspace path behaviour."""

from pathlib import Path

from cursor2telegram.config import Config, CursorConfig, StorageConfig, TelegramConfig
from cursor2telegram.sessions import ChatSession, SessionManager


def test_workspace_remapped_when_configured_as_root(tmp_path):
    root = tmp_path / "state"
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace="/root"),
        storage=StorageConfig(workspaces_root=root / "workspaces"),
    )
    sm = SessionManager(cfg)
    p = sm._workspace_for(42)
    assert p == root / "workspaces" / "chat-42"
    assert p.is_dir()


def test_workspace_keeps_normal_path(tmp_path):
    proj = tmp_path / "myapp"
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=str(proj)),
        storage=StorageConfig(workspaces_root=tmp_path / "workspaces"),
    )
    sm = SessionManager(cfg)
    out = sm._workspace_for(1)
    assert out == proj
    assert out.is_dir()


def test_is_unsafe_flags_slash_root_and_detects_logical_root_path():
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(),
        storage=StorageConfig(workspaces_root=Path("/var/lib/ws")),
    )
    sm = SessionManager(cfg)
    assert sm._is_unsafe_cursor_workspace(Path("/"))
    assert sm._is_unsafe_cursor_workspace(Path("/root"))
    assert sm._is_unsafe_cursor_workspace(Path("/root/"))
    assert not sm._is_unsafe_cursor_workspace(Path("/root/myproject"))


def test_subpath_under_root_not_treated_as_filesystem_root(tmp_path):
    proj = tmp_path / "under" / "proj"
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=str(proj)),
        storage=StorageConfig(workspaces_root=tmp_path / "ws"),
    )
    sm = SessionManager(cfg)
    out = sm._workspace_for(1)
    assert out == proj


def test_get_or_create_heals_session_stuck_on_root(tmp_path):
    ws_root = tmp_path / "workspaces"
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=""),
        storage=StorageConfig(workspaces_root=ws_root),
    )
    sm = SessionManager(cfg)
    sm.sessions[7] = ChatSession(chat_id=7, workspace=Path("/root"))
    healed = sm.get_or_create(7)
    assert healed.workspace == ws_root / "chat-7"
    assert healed.workspace.is_dir()
