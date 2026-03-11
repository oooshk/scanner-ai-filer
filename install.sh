#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BASE_DIR/.venv"
CONFIG="$BASE_DIR/config.yaml"

echo "[1/5] Installing apt dependencies"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip tesseract-ocr ocrmypdf poppler-utils cifs-utils samba samba-common-bin acl

echo "[2/5] Creating virtualenv"
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[3/5] Installing python packages"
pip install --upgrade pip
pip install -r "$BASE_DIR/requirements.txt"

echo "[4/5] Creating config"
if [[ ! -f "$CONFIG" ]]; then
  cp "$BASE_DIR/config.example.yaml" "$CONFIG"
  echo "Created $CONFIG. Edit model path and command_template before starting service."
else
  echo "$CONFIG already exists; leaving unchanged"
fi

echo "[5/5] Ensuring runtime directories"
mkdir -p "$BASE_DIR"/{inbox,processing,review,archive,rejected,state}

echo "Install complete"
echo "Next:"
echo "  1) Edit $CONFIG"
echo "  2) Run one-shot test:"
echo "     $VENV_DIR/bin/python -m scanner_filer.cli --config $CONFIG run-once"
echo "  3) Install scanner and web systemd services (see README)"
