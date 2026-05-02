#!/usr/bin/env bash
# scripts/setup-server.sh
# Create the cursoragent service user with passwordless sudo, install Cursor CLI,
# create state directories, and copy default config.
#
# Usage:  sudo bash scripts/setup-server.sh
#
# Idempotent: safe to re-run. Verifies sudoers with visudo before installing.

set -euo pipefail

USERNAME="${C2T_USER:-cursoragent}"
HOMEDIR="/home/${USERNAME}"
STATE_DIR="/var/lib/cursor2telegram"
LOG_DIR="/var/log/cursor2telegram"
CONFIG_DIR="/etc/cursor2telegram"
SUDOERS_FILE="/etc/sudoers.d/90-${USERNAME}"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

echo "==> Ensure user ${USERNAME} exists"
if ! id -u "${USERNAME}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --comment "cursor2telegram service" "${USERNAME}"
else
  echo "    user ${USERNAME} already exists"
fi

echo "==> Configure passwordless sudo for ${USERNAME}"
SUDOERS_TMP="$(mktemp)"
cat >"${SUDOERS_TMP}" <<EOF
# Managed by cursor2telegram setup-server.sh
${USERNAME} ALL=(ALL) NOPASSWD:ALL
Defaults:${USERNAME} !requiretty
Defaults:${USERNAME} env_keep += "CURSOR_API_KEY CURSOR_AUTH_TOKEN CURSOR_ENDPOINT TELEGRAM_BOT_TOKEN"
EOF
chmod 0440 "${SUDOERS_TMP}"
if ! visudo -cf "${SUDOERS_TMP}" >/dev/null; then
  echo "ERROR: sudoers file failed validation; aborting." >&2
  rm -f "${SUDOERS_TMP}"
  exit 2
fi
install -m 0440 -o root -g root "${SUDOERS_TMP}" "${SUDOERS_FILE}"
rm -f "${SUDOERS_TMP}"
echo "    ${SUDOERS_FILE} installed"

echo "==> Create state/log/config directories"
install -d -m 0750 -o "${USERNAME}" -g "${USERNAME}" "${STATE_DIR}"
install -d -m 0755 -o "${USERNAME}" -g "${USERNAME}" "${STATE_DIR}/workspaces"
install -d -m 0750 -o "${USERNAME}" -g "${USERNAME}" "${LOG_DIR}"
install -d -m 0750 -o root -g "${USERNAME}" "${CONFIG_DIR}"

if [[ ! -f "${CONFIG_DIR}/config.toml" ]]; then
  if [[ -f "$(dirname "$0")/../config/config.example.toml" ]]; then
    install -m 0640 -o root -g "${USERNAME}" \
      "$(dirname "$0")/../config/config.example.toml" \
      "${CONFIG_DIR}/config.toml"
    echo "    seeded ${CONFIG_DIR}/config.toml from example (edit it!)"
  fi
fi

if [[ ! -f "${CONFIG_DIR}/env" ]]; then
  if [[ -f "$(dirname "$0")/../config/env.example" ]]; then
    install -m 0640 -o root -g "${USERNAME}" \
      "$(dirname "$0")/../config/env.example" \
      "${CONFIG_DIR}/env"
    echo "    seeded ${CONFIG_DIR}/env (set TELEGRAM_BOT_TOKEN there!)"
  fi
fi

echo "==> Install Cursor CLI for ${USERNAME} (idempotent)"
if ! sudo -u "${USERNAME}" bash -lc 'command -v agent' >/dev/null; then
  sudo -u "${USERNAME}" bash -lc 'curl -fsS https://cursor.com/install | bash' || {
    echo "WARN: cursor install script failed; you can install manually later." >&2
  }
fi
sudo -u "${USERNAME}" bash -lc 'grep -q "\.local/bin" ~/.bashrc || echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc'

if sudo -u "${USERNAME}" bash -lc 'PATH="$HOME/.local/bin:$PATH" command -v agent' >/dev/null; then
  echo "    agent found:"
  sudo -u "${USERNAME}" bash -lc 'PATH="$HOME/.local/bin:$PATH" agent --version' || true
else
  echo "    NOTE: agent not on PATH yet for ${USERNAME}; install manually with"
  echo "          sudo -u ${USERNAME} -i curl -fsS https://cursor.com/install | bash"
fi

echo
echo "Next steps:"
echo "  1) edit ${CONFIG_DIR}/env  and set TELEGRAM_BOT_TOKEN (and optionally CURSOR_API_KEY)"
echo "  2) edit ${CONFIG_DIR}/config.toml and set [telegram] allowed_user_ids"
echo "  3) authenticate cursor as ${USERNAME}:  sudo -u ${USERNAME} -i agent login"
echo "  4) install the python service:  sudo bash scripts/install-service.sh"
echo "  5) start it:                    sudo systemctl enable --now cursor2telegram"
