from cursor2telegram.bot import BotApp
from cursor2telegram.config import Config, CursorConfig, TelegramConfig
from cursor2telegram.telegram_ui import kb_main_menu


def test_all_main_menu_buttons_have_callback_handlers(tmp_path):
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=str(tmp_path / "workspace")),
    )
    app = BotApp(cfg)
    handlers = app._callback_command_handlers()

    callback_names = {
        button.callback_data.split(":", 1)[1]
        for row in kb_main_menu().inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("cmd:")
    }

    assert callback_names
    assert callback_names <= set(handlers)
    assert "model" in handlers
    assert "mode" in handlers
    assert "workspace" in handlers


def test_workspace_from_new_command_args(tmp_path):
    target = tmp_path / "studyshorts"
    parsed = BotApp._workspace_from_args([str(target)])
    assert parsed == target.resolve(strict=False)
    assert BotApp._workspace_from_args([]) is None
    assert BotApp._workspace_from_args(None) is None


def test_voice_summary_enabled_uses_session_override(tmp_path):
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=str(tmp_path / "workspace")),
    )
    app = BotApp(cfg)
    sess = app.sessions.get_or_create(1)

    assert app._voice_summary_enabled(sess)
    sess.extra_state["voice_summary_enabled"] = False
    assert not app._voice_summary_enabled(sess)
    sess.extra_state["voice_summary_enabled"] = True
    assert app._voice_summary_enabled(sess)


def test_model_tokens_are_short_enough_for_telegram(tmp_path):
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=str(tmp_path / "workspace")),
    )
    app = BotApp(cfg)
    long_model_id = "gpt-5.5[context=272k,reasoning=medium,fast=false]"
    token_map = {"m0": long_model_id}
    assert len(f"model:set:{next(iter(token_map))}") <= 64


def test_load_acp_mcp_servers_converts_maps_to_arrays(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cfg_dir = home / ".cursor"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "mcp.json").write_text(
        """
{
  "mcpServers": {
    "browser": {
      "command": "node",
      "args": ["/srv/browser/mcp.js"],
      "cwd": "/srv/browser",
      "env": {"HOME": "/home/cursoragent"}
    },
    "docs": {
      "url": "https://example.com/mcp",
      "headers": {"X-Test": "1"}
    }
  }
}
""".strip()
    )
    monkeypatch.setenv("HOME", str(home))
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=str(tmp_path / "workspace")),
    )
    app = BotApp(cfg)

    servers = app._load_acp_mcp_servers()
    browser = next(s for s in servers if s["name"] == "browser")
    docs = next(s for s in servers if s["name"] == "docs")

    assert browser["env"] == [{"name": "HOME", "value": "/home/cursoragent"}]
    assert browser["args"] == ["/srv/browser/mcp.js"]
    assert docs["type"] == "http"
    assert docs["headers"] == [{"name": "X-Test", "value": "1"}]


def test_extract_file_paths_from_tool_results(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"fake")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    missing = tmp_path / "missing.png"
    cfg = Config(
        telegram=TelegramConfig(allowed_user_ids=(1,)),
        cursor=CursorConfig(workspace=str(tmp_path / "workspace")),
    )
    app = BotApp(cfg)

    paths = app._extract_file_paths(
        {
            "saved": str(image),
            "nested": [{"filePath": str(video)}],
            "stdout": f"created {image} and {missing}",
        }
    )

    assert paths == [image.resolve(), video.resolve()]
