#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_OWNER_USER="${SUDO_USER:-${USER:-$(id -un)}}"
DEFAULT_OWNER_GROUP="$(id -gn "$DEFAULT_OWNER_USER" 2>/dev/null || echo "$DEFAULT_OWNER_USER")"

INBOX_DIR="${INBOX_DIR:-$SCRIPT_DIR/inbox}"
SMB_USER_DEFAULT="${SMB_USER_DEFAULT:-scannerdrop}"
SHARE_NAME="${SHARE_NAME:-scanner_inbox}"
SMB_CONF="${SMB_CONF:-/etc/samba/smb.conf}"
OWNER_USER="${OWNER_USER:-$DEFAULT_OWNER_USER}"
OWNER_GROUP="${OWNER_GROUP:-$DEFAULT_OWNER_GROUP}"
NON_INTERACTIVE=false
SMB_USER=""
SMB_PASS="${SMB_PASS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=true
      shift
      ;;
    --scanner-user)
      SMB_USER="$2"
      shift 2
      ;;
    --password)
      SMB_PASS="$2"
      shift 2
      ;;
    --inbox-dir)
      INBOX_DIR="$2"
      shift 2
      ;;
    --share-name)
      SHARE_NAME="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

echo "Configuring Samba drop share for scanner uploads"

if ! command -v smbd >/dev/null 2>&1; then
  echo "smbd not found. Install Samba first (run ./install.sh)."
  exit 1
fi

if [[ "$NON_INTERACTIVE" != true ]]; then
  read -r -p "Scanner Samba username [$SMB_USER_DEFAULT]: " SMB_USER
  SMB_USER="${SMB_USER:-$SMB_USER_DEFAULT}"

  read -r -s -p "Scanner Samba password: " SMB_PASS
  echo
else
  SMB_USER="${SMB_USER:-$SMB_USER_DEFAULT}"
fi
if [[ -z "$SMB_PASS" ]]; then
  echo "Password cannot be empty"
  exit 1
fi

sudo mkdir -p "$INBOX_DIR"
sudo chown -R "$OWNER_USER:$OWNER_GROUP" "$INBOX_DIR"
sudo chmod 2770 "$INBOX_DIR"

# Ensure a local Linux user exists for Samba auth; no shell login.
if ! id "$SMB_USER" >/dev/null 2>&1; then
  sudo useradd -M -s /usr/sbin/nologin "$SMB_USER"
fi

# Add/refresh Samba password non-interactively.
printf '%s\n%s\n' "$SMB_PASS" "$SMB_PASS" | sudo smbpasswd -s -a "$SMB_USER"
sudo smbpasswd -e "$SMB_USER" >/dev/null

# Give scanner user write access to inbox via ACL while preserving owner ownership.
sudo setfacl -m "u:${SMB_USER}:rwx" "$INBOX_DIR"
sudo setfacl -d -m "u:${SMB_USER}:rwx" "$INBOX_DIR"

if ! grep -q "^\[$SHARE_NAME\]" "$SMB_CONF"; then
  sudo tee -a "$SMB_CONF" >/dev/null <<EOF

[$SHARE_NAME]
   path = $INBOX_DIR
   browseable = yes
   writable = yes
   read only = no
   guest ok = no
   valid users = $SMB_USER
  force user = $OWNER_USER
  force group = $OWNER_GROUP
   create mask = 0660
   directory mask = 2770
EOF
fi

sudo testparm -s >/dev/null
sudo systemctl restart smbd nmbd
sudo systemctl enable smbd nmbd >/dev/null

echo
HOST_IP="$(hostname -I | awk '{print $1}')"
echo "Scanner share is ready"
echo "Share name: $SHARE_NAME"
echo "Scanner network path (Windows format): \\\\$HOST_IP\\$SHARE_NAME"
echo "Scanner username: $SMB_USER"
echo "Inbox on Pi: $INBOX_DIR"
