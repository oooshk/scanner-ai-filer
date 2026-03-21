#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SCANNER_DIR="${SCANNER_DIR:-$REPO_DIR}"
SCANNER_USER="${SCANNER_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"
SCANNER_HOME="${SCANNER_HOME:-$(getent passwd "$SCANNER_USER" | cut -d: -f6)}"
PYTHON_BIN="${PYTHON_BIN:-$SCANNER_DIR/.venv/bin/python}"
CONFIG_PATH="${CONFIG_PATH:-$SCANNER_DIR/config.yaml}"
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
LLM_READY_URL="${LLM_READY_URL:-$OLLAMA_HOST/api/tags}"
LLM_READY_TIMEOUT="${LLM_READY_TIMEOUT:-120}"
HAILO_OLLAMA_ENABLE="${HAILO_OLLAMA_ENABLE:-auto}"
HAILO_OLLAMA_BIN="${HAILO_OLLAMA_BIN:-$(command -v hailo-ollama || true)}"

WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_PORT="${WEB_PORT:-8090}"
WEB_USER="${WEB_USER:-admin}"
WEB_PASSWORD="${WEB_PASSWORD:-}"
WEB_ENABLE_SETUP="${WEB_ENABLE_SETUP:-true}"
WEB_SECURE_COOKIE="${WEB_SECURE_COOKIE:-false}"
WEB_ALLOWED_NETS="${WEB_ALLOWED_NETS:-127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
WEB_DISABLE_AUTH="${WEB_DISABLE_AUTH:-false}"

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

if [[ -z "$SCANNER_HOME" ]]; then
  SCANNER_HOME="/home/$SCANNER_USER"
fi

is_local_hailo_host=false
if [[ "$OLLAMA_HOST" =~ ^https?://(127\.0\.0\.1|localhost):8000/?$ ]]; then
  is_local_hailo_host=true
fi

INSTALL_HAILO_OLLAMA=false
case "${HAILO_OLLAMA_ENABLE,,}" in
  auto|"")
    if [[ "$is_local_hailo_host" == true ]]; then
      INSTALL_HAILO_OLLAMA=true
    fi
    ;;
  1|true|yes)
    INSTALL_HAILO_OLLAMA=true
    ;;
  0|false|no)
    INSTALL_HAILO_OLLAMA=false
    ;;
  *)
    echo "Invalid HAILO_OLLAMA_ENABLE value: $HAILO_OLLAMA_ENABLE (expected auto|true|false)"
    exit 2
    ;;
esac

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python binary not found or not executable: $PYTHON_BIN"
  echo "Set PYTHON_BIN or create the virtualenv first."
  exit 1
fi

if [[ "$INSTALL_HAILO_OLLAMA" == true && ( -z "$HAILO_OLLAMA_BIN" || ! -x "$HAILO_OLLAMA_BIN" ) ]]; then
  echo "Hailo-Ollama service requested but binary not found: ${HAILO_OLLAMA_BIN:-<empty>}"
  echo "Install hailo-ollama first or set HAILO_OLLAMA_BIN explicitly."
  exit 1
fi

render_template() {
  local src="$1"
  local dst="$2"

  sed \
    -e "s|{{SCANNER_DIR}}|$SCANNER_DIR|g" \
    -e "s|{{SCANNER_USER}}|$SCANNER_USER|g" \
    -e "s|{{SCANNER_HOME}}|$SCANNER_HOME|g" \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    -e "s|{{CONFIG_PATH}}|$CONFIG_PATH|g" \
    -e "s|{{OLLAMA_HOST}}|$OLLAMA_HOST|g" \
    -e "s|{{LLM_READY_URL}}|$LLM_READY_URL|g" \
    -e "s|{{LLM_READY_TIMEOUT}}|$LLM_READY_TIMEOUT|g" \
    -e "s|{{HAILO_OLLAMA_BIN}}|$HAILO_OLLAMA_BIN|g" \
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
tmp_hailo="$(mktemp)"
trap 'rm -f "$tmp_cli" "$tmp_web" "$tmp_hailo"' EXIT

render_template "$REPO_DIR/systemd/scanner-filer.service" "$tmp_cli"
render_template "$REPO_DIR/systemd/scanner-filer-web.service" "$tmp_web"
if [[ "$INSTALL_HAILO_OLLAMA" == true ]]; then
  render_template "$REPO_DIR/systemd/hailo-ollama.service" "$tmp_hailo"
fi

echo "Installing rendered unit files to $SYSTEMD_DIR"
sudo install -m 0644 "$tmp_cli" "$SYSTEMD_DIR/scanner-filer.service"
sudo install -m 0644 "$tmp_web" "$SYSTEMD_DIR/scanner-filer-web.service"
if [[ "$INSTALL_HAILO_OLLAMA" == true ]]; then
  sudo install -m 0644 "$tmp_hailo" "$SYSTEMD_DIR/hailo-ollama.service"
fi

sudo systemctl daemon-reload
if [[ "$INSTALL_HAILO_OLLAMA" == true ]]; then
  sudo systemctl enable --now hailo-ollama
  sudo systemctl restart hailo-ollama
fi
sudo systemctl enable --now scanner-filer scanner-filer-web
sudo systemctl restart scanner-filer scanner-filer-web

status_units=(scanner-filer scanner-filer-web)
if [[ "$INSTALL_HAILO_OLLAMA" == true ]]; then
  status_units=(hailo-ollama "${status_units[@]}")
fi
sudo systemctl status "${status_units[@]}" --no-pager -n 8

echo
echo "Installed scanner services with:"
echo "  SCANNER_DIR=$SCANNER_DIR"
echo "  SCANNER_USER=$SCANNER_USER"
echo "  SCANNER_HOME=$SCANNER_HOME"
echo "  CONFIG_PATH=$CONFIG_PATH"
echo "  OLLAMA_HOST=$OLLAMA_HOST"
echo "  LLM_READY_URL=$LLM_READY_URL"
echo "  LLM_READY_TIMEOUT=$LLM_READY_TIMEOUT"
echo "  HAILO_OLLAMA_ENABLE=$INSTALL_HAILO_OLLAMA"
if [[ "$INSTALL_HAILO_OLLAMA" == true ]]; then
  echo "  HAILO_OLLAMA_BIN=$HAILO_OLLAMA_BIN"
fi
echo "  WEB_HOST=$WEB_HOST"
echo "  WEB_PORT=$WEB_PORT"
