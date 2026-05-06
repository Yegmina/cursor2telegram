from pathlib import Path

from cursor2telegram.config import load_config


def test_load_config_defaults(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        """
[telegram]
allowed_user_ids = [42, 7]

[cursor]
default_mode = "ask"
default_model = "auto"

[ux]
stream_throttle_ms = 250
use_message_drafts = false

[voice]
enabled = true
summary_enabled = false
tts_voice = "nova"
""".strip()
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-from-env")
    monkeypatch.delenv("CURSOR2TELEGRAM_APPROVE_MCPS", raising=False)
    cfg = load_config(cfg_path)
    assert cfg.telegram.bot_token == "tok-from-env"
    assert cfg.telegram.allowed_user_ids == (42, 7)
    assert cfg.cursor.default_mode == "ask"
    assert cfg.cursor.default_model == "auto"
    assert cfg.ux.stream_throttle_ms == 250
    assert not cfg.ux.use_message_drafts
    assert cfg.voice.enabled
    assert not cfg.voice.summary_enabled
    assert cfg.voice.tts_voice == "nova"
    assert cfg.cursor.acp_ready_delay_s == 2.0
    assert cfg.cursor.approve_mcps
    assert cfg.is_allowed(42, None)
    assert not cfg.is_allowed(99, None)


def test_load_config_no_files(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CURSOR2TELEGRAM_APPROVE_MCPS", raising=False)
    cfg = load_config(Path("/nonexistent/path.toml"))
    assert cfg.telegram.bot_token == ""
    assert cfg.telegram.allowed_user_ids == ()
    assert cfg.voice.provider == "openai"
    assert cfg.voice.summary_enabled
    assert cfg.cursor.approve_mcps
    assert not cfg.is_allowed(1, 1)


def test_acp_ready_delay_from_toml_and_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        """
[telegram]
allowed_user_ids = [1]

[cursor]
acp_ready_delay_s = 3.5
""".strip()
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    cfg = load_config(cfg_path)
    assert cfg.cursor.acp_ready_delay_s == 3.5

    monkeypatch.setenv("CURSOR2TELEGRAM_ACP_READY_DELAY_S", "0.5")
    cfg2 = load_config(cfg_path)
    assert cfg2.cursor.acp_ready_delay_s == 0.5


def test_approve_mcps_from_toml_and_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        """
[telegram]
allowed_user_ids = [1]

[cursor]
approve_mcps = true
""".strip()
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.delenv("CURSOR2TELEGRAM_APPROVE_MCPS", raising=False)
    assert load_config(cfg_path).cursor.approve_mcps

    cfg_path.write_text(
        """
[telegram]
allowed_user_ids = [1]

[cursor]
approve_mcps = false
""".strip()
    )
    assert not load_config(cfg_path).cursor.approve_mcps

    monkeypatch.setenv("CURSOR2TELEGRAM_APPROVE_MCPS", "true")
    assert load_config(cfg_path).cursor.approve_mcps
    monkeypatch.setenv("CURSOR2TELEGRAM_APPROVE_MCPS", "0")
    assert not load_config(cfg_path).cursor.approve_mcps


def test_resolved_agent_binary_explicit(monkeypatch, tmp_path):
    bin_path = tmp_path / "fake-agent"
    bin_path.write_text("#!/bin/sh\nexit 0\n")
    bin_path.chmod(0o755)
    monkeypatch.setenv("CURSOR_AGENT_BIN", str(bin_path))
    cfg = load_config()
    assert cfg.resolved_agent_binary() == str(bin_path)
