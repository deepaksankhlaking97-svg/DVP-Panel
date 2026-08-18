#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then echo "Run as root."; exit 1; fi

systemctl disable --now dockervps.service 2>/dev/null || true
rm -f /etc/systemd/system/dockervps.service
systemctl daemon-reload
rm -rf /opt/dockervps

echo "DVP Panel files and service removed."
echo "Docker itself was NOT removed."
