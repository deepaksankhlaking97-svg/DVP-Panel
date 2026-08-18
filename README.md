# DVP Panel

This package contains the supplied current `app.py` plus a fresh-install wrapper.

## Install

```bash
chmod +x install.sh repair.sh uninstall.sh
./install.sh
```

The installer installs Docker, Python, tmate and the required Python packages, creates a fresh database schema, installs `dockervps.service`, and starts the panel on port `8080`.

## Default admin

- Username: `admin`
- Password: `Admin213`

The fresh database contains the admin account but no previous VPS/container records.

## Service

```bash
systemctl status dockervps.service
journalctl -u dockervps.service -f
```

Panel: `http://SERVER-IP:8080/dashboard`

## Repair

```bash
./repair.sh
```

## Uninstall

```bash
./uninstall.sh
```

The uninstall script removes the panel and its systemd service, but intentionally leaves Docker installed.
