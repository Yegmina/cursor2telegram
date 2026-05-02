#!/usr/bin/env bash
# Configure MCP servers for the cursor2telegram service user.
#
# This mirrors the root Cursor MCP setup and adds a headless Browser MCP server.
# It is intentionally idempotent.

set -euo pipefail

USERNAME="${C2T_USER:-cursoragent}"
HOME_DIR="/home/${USERNAME}"
MCP_ROOT="/opt/cursor2telegram/mcp"
BROWSER_SRC="${BROWSER_SRC:-/root/.cursor/plugins/cache/cursor-public/browse/release_v0.2.4}"
TELEGRAM_BIN_SRC="${TELEGRAM_BIN_SRC:-/root/.cursor/bin/telegram-mcp}"
TG_SESSION_SRC="${TG_SESSION_SRC:-/root/.telegram-mcp/session-yehorold.json}"
TG_MAIN_SESSION_SRC="${TG_MAIN_SESSION_SRC:-/root/.telegram-mcp/session.json}"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

if ! id -u "${USERNAME}" >/dev/null 2>&1; then
  echo "ERROR: user ${USERNAME} does not exist. Run scripts/setup-server.sh first." >&2
  exit 2
fi

echo "==> Stage Browser MCP"
if [[ ! -d "${BROWSER_SRC}" ]]; then
  echo "ERROR: Browser MCP source not found at ${BROWSER_SRC}" >&2
  exit 3
fi
install -d -m 0755 -o root -g root "${MCP_ROOT}/browser" "${MCP_ROOT}/bin"
rsync -a --delete "${BROWSER_SRC}/" "${MCP_ROOT}/browser/"

# The Browse CLI defaults to headed mode; servers usually have no DISPLAY.
python3 - <<PY
from pathlib import Path
p = Path("${MCP_ROOT}/browser/dist/src/mcp-server.js")
s = p.read_text()
old = "await execFileAsync(BROWSE_BIN, ['--json', ...args], {"
new = "await execFileAsync(BROWSE_BIN, ['--headless', '--json', ...args], {"
if old in s:
    p.write_text(s.replace(old, new))
PY

echo "==> Stage Telegram MCP"
install -m 0755 -o root -g root "${TELEGRAM_BIN_SRC}" "${MCP_ROOT}/bin/telegram-mcp"
cat >"${MCP_ROOT}/telegram-mcp-cursor.js" <<'JS'
"use strict";
const { spawn } = require("child_process");

const argv = process.argv.slice(2).filter((a) => a !== "stdio" && a !== "--stdio");
const binary = process.env.TELEGRAM_MCP_BINARY || "/opt/cursor2telegram/mcp/bin/telegram-mcp";
const child = spawn(binary, argv, { stdio: "inherit", env: process.env });

child.on("error", (err) => {
  console.error(err);
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) process.exit(1);
  process.exit(code == null ? 1 : code);
});
JS
chmod 0755 "${MCP_ROOT}/telegram-mcp-cursor.js"

install -d -m 0750 -o "${USERNAME}" -g "${USERNAME}" "${HOME_DIR}/.telegram-mcp" "${HOME_DIR}/.cursor"
install -m 0640 -o "${USERNAME}" -g "${USERNAME}" "${TG_SESSION_SRC}" "${HOME_DIR}/.telegram-mcp/session-yehorold.json"
if [[ -f "${TG_MAIN_SESSION_SRC}" ]]; then
  install -m 0640 -o "${USERNAME}" -g "${USERNAME}" "${TG_MAIN_SESSION_SRC}" "${HOME_DIR}/.telegram-mcp/session.json"
fi

echo "==> Write ${HOME_DIR}/.cursor/mcp.json"
cat >"${HOME_DIR}/.cursor/mcp.json" <<'JSON'
{
  "mcpServers": {
    "Figma": {
      "url": "https://mcp.figma.com/mcp",
      "headers": {}
    },
    "openaiDeveloperDocs": {
      "url": "https://developers.openai.com/mcp",
      "headers": {}
    },
    "YehorOldFi": {
      "command": "node",
      "args": ["/opt/cursor2telegram/mcp/telegram-mcp-cursor.js"],
      "env": {
        "TELEGRAM_MCP_BINARY": "/opt/cursor2telegram/mcp/bin/telegram-mcp",
        "TG_APP_ID": "36894616",
        "TG_API_HASH": "cdf6e443869c4e59c43d1c72fbda217c",
        "TG_SESSION_PATH": "/home/cursoragent/.telegram-mcp/session-yehorold.json"
      }
    },
    "telegramMainFi": {
      "command": "node",
      "args": ["/opt/cursor2telegram/mcp/telegram-mcp-cursor.js"],
      "env": {
        "TELEGRAM_MCP_BINARY": "/opt/cursor2telegram/mcp/bin/telegram-mcp",
        "TG_APP_ID": "36894616",
        "TG_API_HASH": "cdf6e443869c4e59c43d1c72fbda217c",
        "TG_SESSION_PATH": "/home/cursoragent/.telegram-mcp/session.json"
      }
    },
    "browser": {
      "command": "node",
      "args": ["/opt/cursor2telegram/mcp/browser/dist/src/mcp-server.js"],
      "cwd": "/opt/cursor2telegram/mcp/browser",
      "env": {
        "HOME": "/home/cursoragent"
      }
    }
  }
}
JSON
chown "${USERNAME}:${USERNAME}" "${HOME_DIR}/.cursor/mcp.json"
chmod 0600 "${HOME_DIR}/.cursor/mcp.json"

echo "==> Validate"
sudo -u "${USERNAME}" bash -lc 'PATH="$HOME/.local/bin:$PATH" agent --approve-mcps mcp list-tools browser | sed -n "1,40p"'

echo "Done. Restart cursor2telegram to load approve_mcps/config changes."
