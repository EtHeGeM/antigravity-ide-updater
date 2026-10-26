# Antigravity IDE Updater for Linux

Automatic updates and release notifications for the tarball installation of **Google Antigravity IDE** on Linux.

> **Disclaimer:** This is an unofficial, community-maintained project. It is not affiliated with, endorsed by, or sponsored by Google.

---

## The Problem

On Windows, Antigravity IDE includes a built-in background updater. On Linux, however:
- The IDE is distributed as a standalone `.tar.gz` bundle extracted into a user folder (e.g., `~/İndirilenler/Antigravity IDE` or `~/Downloads/Antigravity IDE`).
- The internal auto-updater is disabled in `product.json`.
- Distribution package repositories (APT, RPM, Arch) do not package or update the official binary.
- Linux users must manually check download pages, download multi-hundred megabyte tarballs, and replace their installation directories.

**Antigravity IDE Updater** solves this with an automated, secure, and reliable updater designed specifically for Linux desktop environments using standard `systemd` user timers.

---

## Key Features

- **Zero Third-Party Python Dependencies:** Built strictly using the Python standard library.
- **Safe Replacement & Rollback:** Prepares candidates on the destination filesystem and swaps directories using same-filesystem renames. Automatically rolls back to the previous version if swap or version verification fails.
- **Non-Destructive Backups:** Previous backups (`<InstallDir>.bak`) are never deleted before a new recovery point is confirmed healthy.
- **Interrupted Installation Recovery:** Detects crashes between filesystem operations and automatically restores from recovery points on the next run.
- **Path Security Checks (`assert_safe`):** Prevents accidental operations on system paths (`/`, `/usr`, `/etc`, `/var`, `/bin`, etc.) and user configuration directories (`~/.config`, `~/.antigravity`, `~/.gemini`, `~/.local`).
- **Secure HTTPS Downloads & Safe Extraction:** Enforces HTTPS on trusted Google CDN domains (`*.google.com`, `*.gvt1.com`, `*.googleapis.com`, `antigravity.google`), validates redirects, enforces size limits, and sanitizes tarball extraction against path traversal (Zip Slip), symlink escapes, hardlink escapes, and special device nodes.
- **Concurrency Protection:** Uses Linux `fcntl.flock` to guarantee that two updater instances never modify files simultaneously.
- **Process-Aware Staging:** Inspects `/proc` to detect running IDE processes (including handles with `(deleted)` binaries) and stages downloads safely until the IDE is closed.
- **Decoupled Architecture:** Direct HTTPS release discovery does not depend on Chrome. Headless rendering is used strictly as a fallback or for changelog parsing; changelog failures never block updates.
- **Systemd User Integration:** Runs rootless under `systemd --user` with persistent daily timers and journal logging.

---

## Supported Environments & Requirements

- **Linux Distribution:** Tested on **Ubuntu 24.04 LTS (Noble Numbat)** (x86_64). Should theoretically work on other modern systemd-based Linux distributions (Debian, Fedora, Arch, openSUSE), but they are not actively tested.
- **Python Version:** Python **3.10, 3.11, 3.12, or 3.13**.
- **System Dependencies:**
  - `systemd` (with user session support).
  - `libnotify-bin` (provides `notify-send` for desktop alerts; optional).
  - `google-chrome` or `chromium` (optional; only needed if formatted changelog previews are desired).

---

## Quick Start / Installation

### 1. Clone the repository
```bash
git clone https://github.com/EtHeGeM/antigravity-ide-updater.git
cd antigravity-ide-updater
```

### 2. Install the automated user timer
```bash
python3 antigravity_updater.py --install
```

This command:
1. Copies the updater scripts to `~/.local/share/antigravity-ide-updater/`.
2. Generates systemd user service and timer definitions (`antigravity-ide-updater.service` and `antigravity-ide-updater.timer`).
3. Reloads the user systemd daemon and activates the timer immediately.

### 3. Verify status
```bash
# Check timer schedule
systemctl --user list-timers antigravity-ide-updater.timer

# View recent updater logs
journalctl --user -u antigravity-ide-updater.service -n 20 --no-pager
```

---

## Command-Line Usage

The updater provides several inspection and simulation flags:

```bash
# Check for updates without downloading or installing
python3 antigravity_updater.py --check

# Dry-run: explain planned actions without modifying filesystem
python3 antigravity_updater.py --dry-run

# Display current installation, staged updates, and timer status
python3 antigravity_updater.py --status

# Print updater version
python3 antigravity_updater.py --version

# Run manual update check (downloads & updates if needed)
python3 antigravity_updater.py

# Enable verbose debug logging
python3 antigravity_updater.py -v
```

### Querying Changelogs Only
To query the changelog for specific Antigravity products:
```bash
python3 antigravity_changelog.py ide
python3 antigravity_changelog.py cli
python3 antigravity_changelog.py hub
```

---

## Configuration (Environment Variables)

The updater automatically checks localized paths (`~/İndirilenler/Antigravity IDE` and `~/Downloads/Antigravity IDE`). You can override paths and thresholds using environment variables:

| Variable | Description | Default |
|---|---|---|
| `ANTIGRAVITY_IDE_PATH` | Installation directory of Antigravity IDE | `~/İndirilenler/Antigravity IDE` or `~/Downloads/Antigravity IDE` |
| `ANTIGRAVITY_UPDATER_STATE` | State directory for locks, metadata, and staging | `~/.local/state/antigravity-ide-updater` |
| `ANTIGRAVITY_MAX_DOWNLOAD_SIZE` | Maximum allowed tarball size in bytes | `2147483648` (2 GiB) |
| `ANTIGRAVITY_DOWNLOAD_TIMEOUT` | Network timeout for downloads in seconds | `120` |

*Tip: If you export `ANTIGRAVITY_IDE_PATH` before running `--install`, that path will be baked into the generated systemd user service unit.*

---

## Security Model & Known Limitations

### Security Architecture
- **Transport Security:** All downloads and release metadata requests are strictly HTTPS. Redirects to non-HTTPS endpoints or untrusted hosts are immediately blocked.
- **Trusted Download Hosts:** Downloads are only permitted from verified Google infrastructure: `antigravity.google`, `dl.google.com`, `*.google.com`, `*.gvt1.com`, `*.googleapis.com`, and `*.google`.
- **Extraction Defense:** Archives are parsed and validated entry by entry. Traversal paths (`../`), absolute paths (`/`), symlinks resolving outside the extraction root, hardlinks escaping the sandbox, and device nodes (character/block/FIFO) are rejected.
- **Data Protection:** The updater never touches user configurations (`~/.config`), extensions, or IDE user state (`~/.antigravity`, `~/.gemini`).

### Known Limitations & Integrity Model
- **No Official Cryptographic Signatures or Checksums:** Google does not publish GPG signatures, SHA256 checksums, or hashes on the Antigravity IDE download portal.
- **Transport Security Only:** The updater verifies that download URLs and redirects belong to official Google domains (`antigravity.google`, `*.gvt1.com`, etc.) over HTTPS. While HTTPS protects against network-level tampering in transit, it does not independently prove archive authenticity or provide offline cryptographic proof.
- **Diagnostic SHA256:** The updater computes and logs the SHA256 digest of downloaded archives strictly for diagnostic and troubleshooting purposes. Because there is no official reference checksum, it is not compared against a trusted reference.

---

## Replacement Sequence & Recovery

### Update Sequence
While individual `os.rename()` calls on the same filesystem are atomic POSIX operations, the overall directory replacement involves multiple discrete steps:
1. The update is staged and verified in `~/.local/state/antigravity-ide-updater/pending/<version>`.
2. A candidate copy is prepared on the destination filesystem (`.<InstallDir>.candidate-<pid>`).
3. The existing installation is renamed to a temporary backup (`.<InstallDir>.bak.tmp-<pid>`).
4. The candidate directory is renamed to `<InstallDir>`.
5. The newly placed `<InstallDir>` is verified (`product.json` and version).
6. Once verified, the temporary backup is promoted to `<InstallDir>.bak`, replacing any older backup.

### Rollback & Crash Recovery
- **Verification Failure:** If post-swap version verification fails, the candidate is discarded and `<InstallDir>` is restored from the temporary backup.
- **Interrupted Updates:** If the process is terminated between rename operations, the next updater run detects that `<InstallDir>` is missing or corrupt and restores the previous working version from the temporary backup. Temporary backups are never deleted until the active installation is verified healthy.
- **Limitations:** Multi-step filesystem operations cannot provide absolute transactional atomicity across sudden power failures, but this sequence ensures a working backup is preserved before the active installation is replaced.

### Manual Recovery
If you ever wish to manually revert to the previous version:
```bash
cd "$(dirname "$ANTIGRAVITY_IDE_PATH")"
# If your IDE directory was 'Antigravity IDE':
rm -rf "Antigravity IDE"
mv "Antigravity IDE.bak" "Antigravity IDE"
```

---

## Troubleshooting

### Timer not triggering when logged out
By default, systemd user managers stop when you log out. If you want updates to run while you are logged out:
```bash
loginctl enable-linger $USER
```

### Inspecting logs
```bash
# View last 50 log lines
journalctl --user -u antigravity-ide-updater.service -n 50 --no-pager

# Follow logs in real time
journalctl --user -u antigravity-ide-updater.service -f
```

### Resetting state
To clear staging and cached notifications without affecting the IDE:
```bash
rm -rf ~/.local/state/antigravity-ide-updater/pending
rm -f ~/.local/state/antigravity-ide-updater/state.json
```

---

## Uninstallation

To remove the scheduled timer and delete the updater scripts:
```bash
python3 antigravity_updater.py --uninstall
```

Your Antigravity IDE installation, personal settings, and project workspaces will remain completely untouched.

---

## Running Automated Tests

Run the full unit test suite locally:
```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

The tests run entirely in temporary directories and never modify real IDE installations or system files.

---

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
