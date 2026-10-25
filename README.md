# Antigravity IDE Updater for Linux

Automatic updates and release notifications for the tarball installation of **Google Antigravity IDE** on Linux.

---

## Problem

On Windows, Antigravity IDE comes with a built-in background updater. On Linux, however:
- The IDE is typically installed by extracting a `.tar.gz` bundle into a user folder (e.g., `~/İndirilenler/Antigravity IDE` or `~/Downloads/Antigravity IDE`).
- The internal auto-updater is disabled in `product.json`.
- The apt repositories do not package or update the IDE binary.
- Download links include dynamically generated build IDs that cannot be guessed without rendering the download page.

**Antigravity IDE Updater** solves this by providing automated release detection, version parsing, staging, atomic replacement with rollbacks, and native desktop notifications.

---

## Features

- **Zero Third-Party Python Dependencies:** Built strictly using the Python 3 standard library.
- **Headless Page Parsing:** Uses headless Chrome/Chromium (`--dump-dom`) to reliably fetch dynamic release URLs and changelogs from `antigravity.google`.
- **Architecture Aware:** Automatically detects your CPU architecture (`x86_64` / `arm64`) and downloads the matching build.
- **Process-Aware Staging:** If the IDE is open while an update is found, it downloads and stages the update in `~/.local/state/` and installs it on the next run after the IDE closes.
- **Atomic Replacement & Safety:**
  - Preserves user settings (`~/.config`, `~/.antigravity`, `~/.gemini` are never touched).
  - Backs up the current install to `<InstallDir>.bak`.
  - Verifies extracted `product.json` before swapping directories.
  - Automatically rolls back to the backup if verification fails.
- **Desktop Notifications:** Sends updates, changelog previews, and status alerts using `notify-send`.
- **Systemd User Timer:** One-command installation for automated daily checks with zero root privileges required.

---

## Requirements

- **Linux** (tested on Ubuntu 24.04, works on any modern systemd-based distro)
- **Python 3.12+** (uses standard library `tarfile` extraction filters)
- **Google Chrome** or **Chromium** installed (used for headless DOM rendering)
- **`notify-send`** (provided by `libnotify-bin` on Debian/Ubuntu)
- **systemd** (user session support)

---

## Quick Start / Installation

### 1. Clone the repository
```bash
git clone https://github.com/EtHeGeM/antigravity-ide-updater.git
cd antigravity-ide-updater
```

### 2. Install the automated daily updater
```bash
python3 antigravity_updater.py --install
```

This single command:
1. Copies the updater scripts to `~/.local/share/antigravity-ide-updater/`.
2. Generates and activates a daily user systemd service and timer (`antigravity-ide-updater.timer`).
3. Starts monitoring for updates automatically.

### 3. Verify status
```bash
# Check timer schedule
systemctl --user list-timers antigravity-ide-updater.timer

# View recent updater logs
journalctl --user -u antigravity-ide-updater.service -n 20 --no-pager
```

---

## Manual Usage

You can also run a manual update check at any time:

```bash
python3 antigravity_updater.py
```

### Fetching Changelogs Only
To query the latest changelog without performing an update:
```bash
python3 antigravity_changelog.py ide
# Or other tabs: cli, hub, sdk
python3 antigravity_changelog.py cli
```

---

## Configuration (Environment Variables)

By default, the updater looks for the IDE in:
`~/İndirilenler/Antigravity IDE` (or localized `~/Downloads/Antigravity IDE`).

If your IDE is located elsewhere, you can configure it via environment variables:

| Variable | Description | Default |
|---|---|---|
| `ANTIGRAVITY_IDE_PATH` | Path to the extracted Antigravity IDE directory | `~/İndirilenler/Antigravity IDE` |
| `ANTIGRAVITY_UPDATER_STATE` | Directory where update status and staged files are stored | `~/.local/state/antigravity-ide-updater` |

*Note: If you export `ANTIGRAVITY_IDE_PATH` before running `python3 antigravity_updater.py --install`, the path will be permanently baked into the systemd service.*

---

## Uninstallation

To remove the scheduled timer and delete installed updater scripts:

```bash
python3 antigravity_updater.py --uninstall
```

Your IDE installation and user settings will remain completely untouched.

---

## License

GNU General Public License v3.0. See [LICENSE](LICENSE) for details.
