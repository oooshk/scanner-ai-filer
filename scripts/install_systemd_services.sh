#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SCANNER_DIR="${SCANNER_DIR:-$REPO_DIR}"
SCANNER_USER="${SCANNER_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"
PYTHON_BIN="${PYTHON_BIN:-$SCANNER_DIR/.venv/bin/python}"
CONFIG_PATH="${CONFIG_PATH:-$SCANNER_DIR/config.yaml}"

WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_PORT="${WEB_PORT:-8090}"
WEB_USER="${WEB_USER:-admin}"
WEB_PASSWORD="${WEB_PASSWORD:-}"
WEB_ENABLE_SETUP="${WEB_ENABLE_SETUP:-true}"
WEB_SECURE_COOKIE="${WEB_SECURE_COOKIE:-false}"
WEB_ALLOWED_NETS="${WEB_ALLOWED_NETS:-127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
WEB_DISABLE_AUTH="${WEB_DISABLE_AUTH:-false}"

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python binary not found or not executable: $PYTHON_BIN"
  echo "Set PYTHON_BIN or create the virtualenv first."
  exit 1
fi

render_template() {
  local src="$1"
  local dst="$2"

  sed \
    -e "s|{{SCANNER_DIR}}|$SCANNER_DIR|g" \
    -e "s|{{SCANNER_USER}}|$SCANNER_USER|g" \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    -e "s|{{CONFIG_PATH}}|$CONFIG_PATH|g" \
    -e "s|{{WEB_HOST}}|$WEB_HOST|g" \
    -e "s|{{WEB_PORT}}|$WEB_PORT|g" \
    -e "s|{{WEB_USER}}|$WEB_USER|g" \
    -e "s|{{WEB_PASSWORD}}|$WEB_PASSWORD|g" \
    -e "s|{{WEB_ENABLE_SETUP}}|$WEB_ENABLE_SETUP|g" \
    -e "s|{{WEB_SECURE_COOKIE}}|$WEB_SECURE_COOKIE|g" \
    -e "s|{{WEB_ALLOWED_NETS}}|$WEB_ALLOWED_NETS|g" \
    -e "s|{{WEB_DISABLE_AUTH}}|$WEB_DISABLE_AUTH|g" \
    "$src" > "$dst"
}

tmp_cli="$(mktemp)"
tmp_web="$(mktemp)"
trap 'rm -f "$tmp_cli" "$tmp_web"' EXIT

render_template "$REPO_DIR/systemd/scanner-filer.service" "$tmp_cli"
render_template "$REPO_DIR/systemd/scanner-filer-web.service" "$tmp_web"

echo "Installing rendered unit files to $SYSTEMD_DIR"
sudo install -m 0644 "$tmp_cli" "$SYSTEMD_DIR/scanner-filer.service"
sudo install -m 0644 "$tmp_web" "$SYSTEMD_DIR/scanner-filer-web.service"

sudo systemctl daemon-reload
sudo systemctl enable --now scanner-filer scanner-filer-web
sudo systemctl restart scanner-filer scanner-filer-web

sudo systemctl status scanner-filer scanner-filer-web --no-pager -n 8

echo
echo "Installed scanner services with:"
echo "  SCANNER_DIR=$SCANNER_DIR"
echo "  SCANNER_USER=$SCANNER_USER"
echo "  CONFIG_PATH=$CONFIG_PATH"
echo "  WEB_HOST=$WEB_HOST"
echo "  WEB_PORT=$WEB_PORT"
