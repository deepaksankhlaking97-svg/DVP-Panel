#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/opt/dockervps
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then echo "Run as root."; exit 1; fi

systemctl enable --now docker
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/app.py" "$APP_DIR/app.py"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
python3 -m venv "$APP_DIR/venv" 2>/dev/null || true
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/python" -m py_compile "$APP_DIR/app.py"
cp "$SCRIPT_DIR/systemd/dockervps.service" /etc/systemd/system/dockervps.service
systemctl daemon-reload
systemctl enable --now dockervps.service
sleep 2
systemctl --no-pager --full status dockervps.service || true
