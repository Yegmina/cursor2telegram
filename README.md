# cursor2telegram

Control [Cursor CLI](https://cursor.com/docs/cli) on your server from a Telegram bot, end-to-end.

This service exposes the **full** Cursor agent experience inside Telegram:

- Real-time streaming of the agent's text, tool calls, file edits, shell output, and todos.
- Interactive plan approvals and permission prompts via inline buttons.
- Multiple-choice questions, mode switching (`agent`/`plan`/`ask`), model selection.
- Session lifecycle: new, list, resume, cancel.
- Configurable workspaces, MCP servers, sandbox, and command permissions.
- File transport: upload from Telegram into the workspace, download files/diffs back.
- Telegram-native UX: long output is paginated/sent as documents, not dumped raw.
- Owner-allowlist by default, with optional approval gating per chat.

It runs as a dedicated systemd service under a `cursoragent` Linux user that owns its own Cursor login and `~/.local/bin/agent` install.

## Architecture

```
Telegram <─> python-telegram-bot <─> orchestrator <─> agent acp (stdio JSON-RPC) <─> Cursor backend
                                          │
                                          ├─ session manager (per chat)
                                          ├─ access policy (owner allowlist + confirmations)
                                          ├─ output router (text / files / cards)
                                          └─ workspace + filesystem helpers
```

## Quick start

```bash
# 1) provision the dedicated user, sudoers rule, and CLI
sudo bash scripts/setup-server.sh

# 2) authenticate the service user with Cursor (browser flow recommended on a workstation,
#    or set CURSOR_API_KEY in /etc/cursor2telegram/env)
sudo -u cursoragent agent login        # interactive
# or
sudo install -d -m 750 -o cursoragent -g cursoragent /etc/cursor2telegram
sudo tee /etc/cursor2telegram/env >/dev/null <<'EOF'
TELEGRAM_BOT_TOKEN=...
CURSOR_API_KEY=...
EOF
sudo chmod 640 /etc/cursor2telegram/env
sudo chown root:cursoragent /etc/cursor2telegram/env

# 3) install the python service and systemd unit
sudo bash scripts/install-service.sh

# 4) start
sudo systemctl enable --now cursor2telegram
sudo journalctl -u cursor2telegram -f
```

## Configuration

Default config at `/etc/cursor2telegram/config.toml`. See [`config/config.example.toml`](config/config.example.toml) for the full schema. Highlights:

- `[telegram] allowed_user_ids` — owner allowlist (recommended).
- `[telegram] allowed_chat_ids` — optional chat allowlist.
- `[policy] require_confirmation_for` — list of action types that require a button press (`shell`, `write`, `delete`, `network`, `mcp`).
- `[policy] auto_approve_permissions` — default decision for ACP `session/request_permission` (`prompt`, `allow-once`, `allow-always`, `reject-once`).
- `[cursor] default_mode`, `default_model`, `workspace`, `extra_args`, `force_writes`.
- `[ux] stream_throttle_ms`, `max_message_chars`, `code_as_document_threshold`.

Secrets are read from environment first, then config. Never commit `config/local.toml` or any `.env` file.

## Bot commands

```
/start          welcome, status, quick actions
/help           command list
/status         current session, mode, model, workspace
/sessions       alias for /status (one session per chat)
/new            start a new session (closes the active one)
/cancel         cancel the running prompt
/mode [agent|plan|ask]
/model [name|list]
/models         list available models
/workspace [path]  show or change workspace
/sandbox [on|off]
/yolo [on|off]  toggle force/auto-approve mode for the current session
/permissions    view CLI permissions config
/mcp list|enable|disable|login <id>
/rules          list cursor rules in workspace
/files [path]   browse workspace files
/get <path>     download a file from the workspace
/put            instructions for uploading a file
/run <cmd>      run a shell command in the workspace (owner-only)
/whoami         cursor account info (`agent status`)
/about          versions, environment
/login          run cursor login; relays the device URL
/logout         cursor logout
/settings       inline settings menu
/ping           liveness probe
```

Plain text messages become prompts to the active session. File uploads are saved into the workspace and made available to the agent.

## Security

- Owner-only by default (`allowed_user_ids` must include your Telegram user id).
- The Telegram bot token, Cursor API key, and any other secrets are read from `/etc/cursor2telegram/env` (mode `0640`, owner `root:cursoragent`).
- The dedicated `cursoragent` user is the only account that runs `agent`. It has passwordless sudo intentionally so the agent can perform server administration on your behalf — **only grant Telegram access to people who should have full root**.
- Destructive commands and writes outside the configured workspace can be gated behind confirmation buttons (configurable).

## Testing

- Unit tests in [`tests/`](tests/) cover config loading, the policy engine,
  stream chunking, the Telegram UI helpers, and the ACP client (against a
  mock ACP server, including server-to-client request handling).
- A live smoke-test loop drives a real `@CursorStudyShortsServerBot`
  instance through the Yehor Telegram MCP server (`tg_send` / `tg_dialog`)
  and verifies each command end-to-end.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest         # 22 tests
```

## Authenticating Cursor

Until the `cursoragent` service user logs in to Cursor, ACP-dependent
features (sending prompts to the agent) will respond with a clear
"please authenticate" message. Two options:

```bash
# 1) Browser flow (one-time, recommended).
sudo -u cursoragent -i agent login
# follow the URL it prints from any browser; auth is then persisted.

# 2) Headless via API key.
echo 'CURSOR_API_KEY=...your-key...' | sudo tee -a /etc/cursor2telegram/env
sudo systemctl restart cursor2telegram
```

After this, sending plain text to the bot starts a streaming Cursor
session in the configured workspace, with permission/plan/question
prompts surfaced as inline buttons.

## License

MIT — see [LICENSE](LICENSE).
