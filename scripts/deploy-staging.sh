#!/usr/bin/env bash
# Deploy the staging Docker service from the staging branch.

set -euo pipefail

REPO_URL="${C2T_REPO_URL:-git@github.com:Yegmina/cursor2telegram.git}"
DEPLOY_DIR="${C2T_DEPLOY_DIR:-/opt/cursor2telegram/staging}"
BRANCH="${C2T_BRANCH:-staging}"
COMPOSE_FILE="${C2T_COMPOSE_FILE:-docker-compose.staging.yml}"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

if ! command -v docker >/dev/null; then
  echo "ERROR: docker is not installed." >&2
  exit 2
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose plugin is not installed." >&2
  exit 2
fi

install -d -m 0755 -o root -g root "$(dirname "${DEPLOY_DIR}")"

if [[ ! -d "${DEPLOY_DIR}/.git" ]]; then
  git clone --branch "${BRANCH}" "${REPO_URL}" "${DEPLOY_DIR}"
else
  git -C "${DEPLOY_DIR}" fetch origin "${BRANCH}"
  git -C "${DEPLOY_DIR}" checkout "${BRANCH}"
  git -C "${DEPLOY_DIR}" reset --hard "origin/${BRANCH}"
fi

if [[ ! -f /etc/cursor2telegram/env || ! -f /etc/cursor2telegram/config.toml ]]; then
  echo "ERROR: /etc/cursor2telegram/env and /etc/cursor2telegram/config.toml must exist on the server." >&2
  exit 3
fi

docker compose -f "${DEPLOY_DIR}/${COMPOSE_FILE}" pull --ignore-buildable || true
docker compose -f "${DEPLOY_DIR}/${COMPOSE_FILE}" up -d --remove-orphans
docker compose -f "${DEPLOY_DIR}/${COMPOSE_FILE}" ps
