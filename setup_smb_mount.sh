#!/usr/bin/env bash
set -euo pipefail

NAS_HOST="${NAS_HOST:-}"
NAS_SHARE="${NAS_SHARE:-Public}"
NAS_USER="${NAS_USER:-}"
MOUNT_POINT="${MOUNT_POINT:-/mnt/nas}"
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"
CRED_FILE="${CRED_FILE:-$HOME/.smb-nas}"
SUBDIR="${SUBDIR:-Home Filing}"
NON_INTERACTIVE=false
NAS_PASS="${NAS_PASS:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=true
      shift
      ;;
    --nas-host)
      NAS_HOST="$2"
      shift 2
      ;;
    --nas-share)
      NAS_SHARE="$2"
      shift 2
      ;;
    --nas-user)
      NAS_USER="$2"
      shift 2
      ;;
    --mount-point)
      MOUNT_POINT="$2"
      shift 2
      ;;
    --cred-file)
      CRED_FILE="$2"
      shift 2
      ;;
    --subdir)
      SUBDIR="$2"
      shift 2
      ;;
    --password)
      NAS_PASS="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

echo "This configures SMB mount: //$NAS_HOST/$NAS_SHARE -> $MOUNT_POINT"
if [[ "$NON_INTERACTIVE" != true ]]; then
  read -r -p "SMB username [$NAS_USER]: " input_user
  if [[ -n "${input_user}" ]]; then
    NAS_USER="$input_user"
  fi

  read -r -s -p "SMB password for $NAS_USER: " NAS_PASS
  echo
fi

if [[ -z "${NAS_PASS}" ]]; then
  echo "Password cannot be empty"
  exit 1
fi

cat > "$CRED_FILE" <<EOF
username=$NAS_USER
password=$NAS_PASS
EOF
chmod 600 "$CRED_FILE"

echo "Creating mount point $MOUNT_POINT"
sudo mkdir -p "$MOUNT_POINT"

FSTAB_LINE="//$NAS_HOST/$NAS_SHARE $MOUNT_POINT cifs credentials=$CRED_FILE,uid=$(id -u "$RUN_USER"),gid=$(id -g "$RUN_USER"),iocharset=utf8,vers=3.0,nofail,x-systemd.automount,_netdev 0 0"
if ! grep -Fq "//$NAS_HOST/$NAS_SHARE $MOUNT_POINT cifs" /etc/fstab; then
  echo "Adding /etc/fstab entry"
  echo "$FSTAB_LINE" | sudo tee -a /etc/fstab >/dev/null
else
  echo "fstab entry already exists; skipping"
fi

echo "Mounting all filesystems"
sudo mount -a

TARGET_BASE="$MOUNT_POINT/$SUBDIR"
mkdir -p "$TARGET_BASE/archive" "$TARGET_BASE/review" "$TARGET_BASE/rejected"

echo "SMB setup complete"
echo "Mounted path: $MOUNT_POINT"
echo "Server filing root: $TARGET_BASE"
