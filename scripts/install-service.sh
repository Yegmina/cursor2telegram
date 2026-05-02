#!/usr/bin/env bash
# scripts/install-service.sh
# Install cursor2telegram into a venv and register the systemd unit.
#
# Usage: sudo bash scripts/install-service.sh

set -euo pipefail

USERNAME="${C2T_USER:-cursoragent}"
INSTALL_DIR="/opt/cursor2telegram"
VENV_DIR="${INSTALL_DIR}/venv"
SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_FILE="/etc/systemd/system/cursor2telegram.service"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

echo "==> Stage source into ${INSTALL_DIR}"
install -d -m 0755 -o root -g root "${INSTALL_DIR}"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' \
  --exclude 'dist' --exclude 'build' \
  "${SOURCE_DIR}/" "${INSTALL_DIR}/"
chown -R root:root "${INSTALL_DIR}"

echo "==> Create venv and install dependencies"
if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install --upgrade pip wheel >/dev/null
"${VENV_DIR}/bin/pip" install -e "${INSTALL_DIR}" >/dev/null

echo "==> Install systemd unit"
install -m 0644 "${SOURCE_DIR}/systemd/cursor2telegram.service" "${UNIT_FILE}"
sed -i "s|@VENV@|${VENV_DIR}|g; s|@USER@|${USERNAME}|g; s|@WORKDIR@|${INSTALL_DIR}|g" "${UNIT_FILE}"
systemctl daemon-reload

echo
echo "Unit installed. To start:"
echo "  systemctl enable --now cursor2telegram"
echo "  journalctl -u cursor2telegram -f"
