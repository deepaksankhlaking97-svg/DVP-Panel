#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/dockervps
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root."
  exit 1
fi

echo "== DVP Panel Installer =="
echo "Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip docker.io tmate curl
systemctl enable --now docker

mkdir -p "$APP_DIR"

# Install the exact supplied current application.
cp "$SCRIPT_DIR/app.py" "$APP_DIR/app.py"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
chmod 755 "$APP_DIR/app.py"

# Fresh database: preserve the admin account, but do not carry over VPS/container data.
rm -f "$APP_DIR/panel.db"
python3 - <<'PY'
import sqlite3
p='/opt/dockervps/panel.db'
con=sqlite3.connect(p)
con.executescript('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS admin (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    container_id TEXT NOT NULL,
    image TEXT NOT NULL,
    cpu REAL NOT NULL,
    ram INTEGER NOT NULL,
    pids INTEGER NOT NULL,
    created INTEGER NOT NULL,
    owner_id INTEGER,
    disk INTEGER DEFAULT 30
);
''')
con.commit()
con.close()
PY

python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# Verify the supplied application before installing the service.
"$APP_DIR/venv/bin/python" -m py_compile "$APP_DIR/app.py"

cp "$SCRIPT_DIR/systemd/dockervps.service" /etc/systemd/system/dockervps.service
systemctl daemon-reload
systemctl enable --now dockervps.service
sleep 2

if systemctl is-active --quiet dockervps.service; then
  echo
  echo "========================================"
  echo " DVP Panel installed successfully"
  echo " URL: http://SERVER-IP:8080/dashboard"
  echo " Username: admin"
  echo " Password: Admin213"
  echo " Service: dockervps.service"
  echo "========================================"
else
  echo "DVP service failed to start. Showing logs:"
  journalctl -u dockervps.service -n 80 --no-pager
  exit 1
fi
