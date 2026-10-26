#!/usr/bin/env python3
"""Linux auto-updater for the tarball install of Google Antigravity IDE.

Flow: read installed version -> check latest release -> download & stage
(or install if IDE is not running) -> notify via notify-send.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

from antigravity_changelog import ChangelogError, _render_dom, fetch_changelog

__version__ = "1.0.0"
logger = logging.getLogger("antigravity-updater")

DOWNLOAD_PAGE = "https://antigravity.google/download/linux"
DOWNLOAD_PAGE_DIRECT = "https://antigravity.google/download?os=linux"
LINK_RE = re.compile(
    r"https://[^\s\"'<>]+/antigravity/stable/(\d+(?:\.\d+)+)(?:-[^/]+)?/linux-(x64|arm64|arm|aarch64)/Antigravity(?:%20|\+)IDE\.tar\.gz"
)

HOME = Path.home()
INSTALL_DIR = Path(
    os.environ.get(
        "ANTIGRAVITY_IDE_PATH",
        HOME / "İndirilenler" / "Antigravity IDE"
        if (HOME / "İndirilenler").exists()
        else HOME / "Downloads" / "Antigravity IDE",
    )
).expanduser()

STATE_DIR = Path(os.environ.get("ANTIGRAVITY_UPDATER_STATE", HOME / ".local/state/antigravity-ide-updater")).expanduser()
STATE_FILE = STATE_DIR / "state.json"
PENDING_DIR = STATE_DIR / "pending"
LOCK_FILE = STATE_DIR / "updater.lock"

DEFAULT_MAX_DOWNLOAD_SIZE = int(os.environ.get("ANTIGRAVITY_MAX_DOWNLOAD_SIZE", 2 * 1024 * 1024 * 1024))
DEFAULT_DOWNLOAD_TIMEOUT = int(os.environ.get("ANTIGRAVITY_DOWNLOAD_TIMEOUT", 120))

TRUSTED_HOSTS = {"antigravity.google", "dl.google.com"}
TRUSTED_SUFFIXES = (".google.com", ".googleapis.com", ".gvt1.com", ".google")

FORBIDDEN_PREFIXES = {
    Path("/usr"), Path("/etc"), Path("/boot"), Path("/dev"), Path("/proc"),
    Path("/sys"), Path("/run"), Path("/root"), Path("/bin"), Path("/sbin"),
    Path("/lib"), Path("/lib32"), Path("/lib64"), Path("/var"),
}
FORBIDDEN_ROOTS = {
    Path("/"), Path("/home"), Path("/tmp"), Path("/opt"), Path("/media"),
    Path("/mnt"), Path("/srv"),
}


class UpdateError(RuntimeError):
    """Raised when an update or safety validation operation fails."""


class LockContentionError(UpdateError):
    """Raised when another updater instance holds the process lock."""


# ---------------------------------------------------------------- helpers
def parse_version(v: str) -> tuple[int, ...]:
    if not v or not isinstance(v, str):
        raise ValueError(f"Invalid version string: {v!r}")
    cleaned = v.strip().lstrip("v").split("-")[0].split("+")[0]
    nums = [int(p) for p in cleaned.split(".") if p.isdigit()]
    if nums:
        return tuple(nums)
    digits = [int(x) for x in re.findall(r"\d+", v)]
    if digits:
        return tuple(digits)
    raise ValueError(f"Cannot parse version from {v!r}")


def get_system_arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise UpdateError(f"Unsupported system architecture: {m}")


arch = get_system_arch


def is_valid_install(path: Path) -> bool:
    """Return True if path contains a readable product.json with ideVersion."""
    try:
        return bool(installed_version(Path(path)))
    except Exception:
        return False


def assert_safe(path: Path, require_installed: bool = True) -> None:
    path = Path(path)
    if not str(path).strip() or str(path) == "." or not path.name:
        raise UpdateError("Security: Invalid or empty target path.")
    p = path.resolve()
    for pref in FORBIDDEN_PREFIXES:
        r = pref.resolve()
        if p in (pref, r) or p.is_relative_to(r):
            raise UpdateError(f"Security: Target path inside protected system directory {pref}: {path}")
    for root in FORBIDDEN_ROOTS:
        r = root.resolve()
        if p in (root, r) or r.is_relative_to(p):
            raise UpdateError(f"Security: Target path cannot be system directory {root}: {path}")

    home = HOME.resolve()
    if p in (Path("/home"), home) or path in (Path("/home"), HOME):
        raise UpdateError(f"Security: Target path cannot be home directory: {path}")

    for prot in (home / ".config", home / ".local", home / ".cache", home / ".antigravity", home / ".gemini", home / ".ssh"):
        r = prot.resolve()
        if p == r or path == prot or r.is_relative_to(p):
            raise UpdateError(f"Security: Target path is a protected user directory: {path}")

    if p.parent in (Path("/"), Path("/home")) or not p.parent.is_dir():
        raise UpdateError(f"Security: Installation parent directory invalid: {p.parent}")

    if require_installed and not is_valid_install(p):
        raise UpdateError(f"Security: {path} is not a valid Antigravity IDE installation.")


def is_trusted_url(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url)
        h = (p.hostname or "").lower()
        return p.scheme == "https" and (h in TRUSTED_HOSTS or any(h.endswith(s) for s in TRUSTED_SUFFIXES))
    except Exception:
        return False


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_trusted_url(newurl):
            raise UpdateError(f"Security: Refused redirect to untrusted URL: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProcessLock:
    def __init__(self, lock_file: Path = LOCK_FILE):
        self.lock_file = lock_file
        self.fd: int | None = None

    def __enter__(self):
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self.fd, 0)
            os.write(self.fd, f"{os.getpid()}\n".encode())
            return self
        except (BlockingIOError, OSError) as exc:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise LockContentionError("Another updater instance is already running.") from exc

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def notify(title: str, body: str = "", urgency: str = "normal") -> None:
    logger.info("%s%s", title, f": {body}" if body else "")
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-u", urgency, "-a", "Antigravity Updater", title, body[:400]],
                           check=False, timeout=5)
        except Exception:
            pass


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def notify_once(state: dict, key: str, title: str, body: str = "", urgency: str = "normal") -> None:
    if state.get("notified", {}).get(key):
        return
    notify(title, body, urgency)
    state.setdefault("notified", {})[key] = True
    save_state(state)


# ---------------------------------------------------------------- steps
def installed_version(root: Path = INSTALL_DIR) -> str:
    pj = root / "resources" / "app" / "product.json"
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        ver = data.get("ideVersion")
        if ver and isinstance(ver, str):
            return ver
    except Exception:
        pass
    raise UpdateError(f"Cannot read installed version from {pj}")


def ide_running(root: Path = INSTALL_DIR) -> bool:
    try:
        r = root.resolve()
        if not Path("/proc").is_dir():
            return False
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
                if exe.endswith(" (deleted)"):
                    exe = exe[:-10]
                p = Path(exe).resolve()
                if p == r or p.is_relative_to(r):
                    return True
            except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                continue
    except OSError:
        pass
    return False


def fetch_download_page(timeout: int = 30) -> str:
    try:
        opener = urllib.request.build_opener(SafeRedirectHandler())
        req = urllib.request.Request(DOWNLOAD_PAGE_DIRECT, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            if LINK_RE.search(content):
                return content
    except Exception:
        pass
    try:
        return _render_dom(DOWNLOAD_PAGE, timeout=timeout)
    except Exception as exc:
        raise UpdateError(f"Failed to fetch Antigravity download page: {exc}") from exc


def arch_matches(url_arch: str, sys_arch: str) -> bool:
    u = url_arch.lower()
    return (u in ("x64", "amd64", "x86_64")) if sys_arch == "x64" else (u in ("arm64", "arm", "aarch64"))


def latest_release() -> tuple[str, str]:
    dom = fetch_download_page()
    sys_arch = get_system_arch()
    found = [
        (m.group(1), m.group(0))
        for m in LINK_RE.finditer(dom)
        if arch_matches(m.group(2), sys_arch) and is_trusted_url(m.group(0))
    ]
    if not found:
        raise UpdateError(f"No release found matching architecture {sys_arch}.")
    return max(found, key=lambda x: parse_version(x[0]))


def release_notes(version: str) -> str:
    try:
        return dict(fetch_changelog("ide")).get(version, "")
    except Exception:
        return ""


def download_file(url: str, dest_path: Path, max_size: int, timeout: int) -> str:
    if not is_trusted_url(url):
        raise UpdateError(f"Security: Untrusted URL: {url}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest_path.with_name(f"{dest_path.name}.part-{os.getpid()}")
    hasher = hashlib.sha256()
    total = 0
    try:
        opener = urllib.request.build_opener(SafeRedirectHandler())
        req = urllib.request.Request(url, headers={"User-Agent": "AntigravityIDEUpdater/1.0"})
        with opener.open(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            if cl and int(cl) > max_size:
                raise UpdateError(f"Download size {cl} exceeds limit of {max_size} bytes.")
            with open(temp_dest, "wb") as out:
                while chunk := resp.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_size:
                        raise UpdateError(f"Download exceeded limit of {max_size} bytes.")
                    hasher.update(chunk)
                    out.write(chunk)
        os.rename(temp_dest, dest_path)
    except Exception:
        temp_dest.unlink(missing_ok=True)
        raise
    return hasher.hexdigest()


def extract_tarball_safe(archive_path: Path, target_dir: Path) -> None:
    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:*") as tar:
        for member in tar.getmembers():
            norm = os.path.normpath(member.name)
            if norm.startswith("/") or norm == ".." or norm.startswith(".." + os.sep):
                raise UpdateError(f"Security: Path traversal entry: {member.name}")
            dest = (target_dir / norm).resolve()
            if not dest.is_relative_to(target_dir):
                raise UpdateError(f"Security: Entry points outside target: {member.name}")
            if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                raise UpdateError(f"Security: Unsupported file type: {member.name}")
            if member.issym():
                if member.linkname.startswith("/"):
                    raise UpdateError(f"Security: Absolute symlink: {member.name}")
                link_dest = (dest.parent / member.linkname).resolve()
                if not link_dest.is_relative_to(target_dir):
                    raise UpdateError(f"Security: Escaped symlink: {member.name}")
            if member.islnk():
                if member.linkname.startswith("/"):
                    raise UpdateError(f"Security: Absolute hardlink: {member.name}")
                link_dest = (target_dir / os.path.normpath(member.linkname)).resolve()
                if not link_dest.is_relative_to(target_dir):
                    raise UpdateError(f"Security: Escaped hardlink: {member.name}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(target_dir, filter="data")
        else:
            tar.extractall(target_dir)


def purge_stale_pending(keep_version: str | None = None) -> None:
    if not PENDING_DIR.is_dir():
        return
    for item in PENDING_DIR.iterdir():
        if item.name.startswith(".tmp-stage-") or (item.is_dir() and item.name != keep_version):
            shutil.rmtree(item, ignore_errors=True)


def verify_pending(version: str) -> Path:
    target = PENDING_DIR / version
    roots = [p.parents[2] for p in (target / "x").glob("**/resources/app/product.json")] if target.is_dir() else []
    if len(roots) == 1 and parse_version(installed_version(roots[0])) == parse_version(version):
        return roots[0]
    shutil.rmtree(target, ignore_errors=True)
    raise UpdateError(f"No valid staged update for v{version}.")


def download_and_extract(url: str, version: str) -> Path:
    temp_stage = PENDING_DIR / f".tmp-stage-{version}-{os.getpid()}"
    shutil.rmtree(temp_stage, ignore_errors=True)
    temp_stage.mkdir(parents=True, exist_ok=True)
    archive = temp_stage / "ide.tar.gz"
    try:
        logger.info("Downloading v%s (%s)", version, url)
        # Note: SHA256 is computed and logged for diagnostic purposes only (Google does not publish reference hashes)
        digest = download_file(url, archive, DEFAULT_MAX_DOWNLOAD_SIZE, DEFAULT_DOWNLOAD_TIMEOUT)
        logger.info("Downloaded archive SHA256: %s (diagnostic only)", digest)
        extract_tarball_safe(archive, temp_stage / "x")
    except Exception as exc:
        shutil.rmtree(temp_stage, ignore_errors=True)
        raise UpdateError(f"Download/extraction failed: {exc}") from exc
    finally:
        archive.unlink(missing_ok=True)

    final_stage = PENDING_DIR / version
    shutil.rmtree(final_stage, ignore_errors=True)
    os.rename(temp_stage, final_stage)
    purge_stale_pending(keep_version=version)
    return verify_pending(version)


def recover_interrupted_install(install_dir: Path = INSTALL_DIR) -> bool:
    """If install_dir is missing or invalid, recover from backup. Never deletes backups if install is broken."""
    parent = install_dir.parent
    if not parent.exists():
        return False

    temp_backups = sorted(parent.glob(f".{install_dir.name}.bak.tmp-*"))
    old_backups = sorted(parent.glob(f".{install_dir.name}.bak.old-*"))
    backup_dir = install_dir.with_name(f"{install_dir.name}.bak")

    # If installation is missing or corrupted, attempt recovery by evaluating all available backups
    if not is_valid_install(install_dir):
        sources = list(reversed(temp_backups)) + ([backup_dir] if backup_dir.exists() else []) + list(reversed(old_backups))
        for source in sources:
            if is_valid_install(source):
                logger.warning("Invalid or missing installation detected. Restoring from %s...", source)
                if install_dir.exists():
                    shutil.rmtree(install_dir, ignore_errors=True)
                os.rename(source, install_dir)
                return True

    # Only clean temporary artifacts when the active installation is confirmed healthy
    if is_valid_install(install_dir):
        if not backup_dir.exists():
            # Promote newest valid temporary backup to official backup if missing
            for candidate in list(reversed(temp_backups)) + list(reversed(old_backups)):
                if is_valid_install(candidate):
                    logger.info("Securing missing backup from %s...", candidate)
                    os.rename(candidate, backup_dir)
                    break
        for cand in parent.glob(f".{install_dir.name}.candidate-*"):
            shutil.rmtree(cand, ignore_errors=True)
        for tb in parent.glob(f".{install_dir.name}.bak.tmp-*"):
            shutil.rmtree(tb, ignore_errors=True)
        for ob in parent.glob(f".{install_dir.name}.bak.old-*"):
            shutil.rmtree(ob, ignore_errors=True)

    return False


def install(new_root: Path, version: str) -> None:
    if ide_running(INSTALL_DIR):
        raise UpdateError("Cannot install while Antigravity IDE is running.")
    try:
        if parse_version(installed_version(INSTALL_DIR)) == parse_version(version):
            shutil.rmtree(PENDING_DIR / version, ignore_errors=True)
            return
    except Exception:
        pass

    assert_safe(INSTALL_DIR, require_installed=True)
    parent = INSTALL_DIR.parent
    candidate = parent / f".{INSTALL_DIR.name}.candidate-{os.getpid()}"
    temp_backup = parent / f".{INSTALL_DIR.name}.bak.tmp-{os.getpid()}"
    backup_dir = INSTALL_DIR.with_name(f"{INSTALL_DIR.name}.bak")

    shutil.rmtree(candidate, ignore_errors=True)
    try:
        shutil.copytree(str(new_root), str(candidate), symlinks=True)
        if parse_version(installed_version(candidate)) != parse_version(version):
            raise UpdateError("Candidate version mismatch.")
    except Exception as exc:
        shutil.rmtree(candidate, ignore_errors=True)
        raise UpdateError(f"Failed to prepare candidate: {exc}") from exc

    if ide_running(INSTALL_DIR):
        shutil.rmtree(candidate, ignore_errors=True)
        raise UpdateError("IDE started during installation preparation.")

    # Swap directories on the same filesystem
    os.rename(INSTALL_DIR, temp_backup)
    try:
        os.rename(candidate, INSTALL_DIR)
        if parse_version(installed_version(INSTALL_DIR)) != parse_version(version):
            raise UpdateError("Installed version verification failed.")
    except Exception as exc:
        logger.error("Swap failed (%s). Rolling back...", exc)
        if INSTALL_DIR.exists():
            shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        if temp_backup.exists():
            os.rename(temp_backup, INSTALL_DIR)
        shutil.rmtree(candidate, ignore_errors=True)
        raise UpdateError(f"Installation failed, rolled back: {exc}") from exc

    # Backup rotation: preserve previous backup until new backup is safely secured
    old_backup = parent / f".{INSTALL_DIR.name}.bak.old-{os.getpid()}"
    shutil.rmtree(old_backup, ignore_errors=True)
    if backup_dir.exists():
        assert_safe(backup_dir, require_installed=False)
        os.rename(backup_dir, old_backup)
    try:
        os.rename(temp_backup, backup_dir)
    except Exception as exc:
        if old_backup.exists() and not backup_dir.exists():
            os.rename(old_backup, backup_dir)
        raise UpdateError(f"Failed to secure backup directory: {exc}") from exc
    if old_backup.exists():
        shutil.rmtree(old_backup, ignore_errors=True)

    shutil.rmtree(PENDING_DIR / version, ignore_errors=True)
    logger.info("Successfully updated Antigravity IDE to v%s", version)


# ---------------------------------------------------------------- commands
def check_updates() -> int:
    """Read-only: check for updates without any filesystem modifications."""
    current = installed_version()
    logger.info("Checking for updates (Current: v%s)...", current)
    latest, url = latest_release()
    if parse_version(latest) <= parse_version(current):
        print(f"Antigravity IDE is up to date (v{current}).")
        return 0
    print(f"Update available: v{current} -> v{latest}\nURL: {url}")
    notes = release_notes(latest)
    if notes:
        print(f"\nRelease Notes:\n{notes}")
    return 0


def dry_run() -> int:
    """Read-only: describe planned actions without any filesystem modifications."""
    current = installed_version()
    latest, url = latest_release()
    logger.info("[Dry Run] Current: v%s at %s", current, INSTALL_DIR)
    logger.info("[Dry Run] Latest:  v%s (%s)", latest, url)
    if parse_version(latest) <= parse_version(current):
        logger.info("[Dry Run] Installation is up to date.")
        return 0
    running = ide_running(INSTALL_DIR)
    logger.info("[Dry Run] IDE running: %s -> Plan: %s", running,
                f"Stage v{latest} in pending/" if running else f"Download v{latest} and swap directories.")
    return 0


def show_status() -> int:
    """Read-only: display installation and timer status."""
    print("=== Antigravity IDE Updater Status ===")
    print(f"Updater Version:  {__version__}")
    print(f"Installation Dir: {INSTALL_DIR}")
    print(f"State Directory:  {STATE_DIR}")
    try:
        print(f"Installed IDE:    v{installed_version(INSTALL_DIR)}")
    except Exception as exc:
        print(f"Installed IDE:    Not found ({exc})")
    print(f"IDE Running:      {'Yes' if ide_running(INSTALL_DIR) else 'No'}")

    staged = []
    if PENDING_DIR.is_dir():
        for p in PENDING_DIR.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                try:
                    verify_pending(p.name)
                    staged.append(p.name)
                except Exception:
                    pass
    print(f"Staged Updates:   {', '.join('v' + v for v in staged) if staged else 'None'}")

    if shutil.which("systemctl"):
        act = subprocess.run(["systemctl", "--user", "is-active", f"{UNIT}.timer"], capture_output=True, text=True, check=False).stdout.strip()
        enb = subprocess.run(["systemctl", "--user", "is-enabled", f"{UNIT}.timer"], capture_output=True, text=True, check=False).stdout.strip()
        print(f"Systemd Timer:    {act} (enabled: {enb})")
    return 0


def run() -> int:
    try:
        with ProcessLock():
            recover_interrupted_install()
            state = load_state()
            current = installed_version()
            logger.info("Installation target: %s (v%s)", INSTALL_DIR, current)
            try:
                latest, url = latest_release()
            except (UpdateError, ChangelogError) as exc:
                notify("Antigravity update check failed", str(exc), "critical")
                return 1

            if parse_version(latest) <= parse_version(current):
                logger.info("Antigravity IDE is up to date.")
                shutil.rmtree(PENDING_DIR, ignore_errors=True)
                return 0

            notes = release_notes(latest)
            notify_once(state, f"found:{latest}", f"Antigravity IDE v{latest} available",
                        f"Installed: v{current}\n\n{notes}" if notes else f"Installed: v{current}")

            try:
                new_root = verify_pending(latest)
            except UpdateError:
                new_root = download_and_extract(url, latest)

            if ide_running(INSTALL_DIR):
                notify_once(state, f"staged:{latest}", f"Antigravity IDE v{latest} ready",
                            "Update will be installed automatically on the next check after IDE closes.")
                return 0

            install(new_root, latest)
            notify(f"Antigravity IDE v{latest} installed", f"v{current} → v{latest}\nBackup: {INSTALL_DIR.name}.bak")
            state["installed"] = latest
            save_state(state)
            return 0
    except LockContentionError as exc:
        logger.info(str(exc))
        return 0


# ---------------------------------------------------------------- systemd
UNIT = "antigravity-ide-updater"
APP_DIR = HOME / ".local/share" / UNIT
UNIT_DIR = HOME / ".config/systemd/user"


def install_timer() -> int:
    if not shutil.which("systemctl"):
        logger.error("systemctl not found.")
        return 1

    src = Path(__file__).resolve().parent
    APP_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("antigravity_updater.py", "antigravity_changelog.py"):
        shutil.copy2(src / name, APP_DIR / name)

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    py_exec = str(sys.executable).replace('\\', '\\\\').replace('"', '\\"')
    script_exec = str(APP_DIR / "antigravity_updater.py").replace('\\', '\\\\').replace('"', '\\"')
    ide_p = str(INSTALL_DIR).replace('\\', '\\\\').replace('"', '\\"')
    state_p = str(STATE_DIR).replace('\\', '\\\\').replace('"', '\\"')

    (UNIT_DIR / f"{UNIT}.service").write_text(f"""[Unit]
Description=Antigravity IDE updater
Documentation=https://github.com/EtHeGeM/antigravity-ide-updater
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment="ANTIGRAVITY_IDE_PATH={ide_p}"
Environment="ANTIGRAVITY_UPDATER_STATE={state_p}"
ExecStart="{py_exec}" "{script_exec}"
StandardOutput=journal
StandardError=journal
""", encoding="utf-8")

    (UNIT_DIR / f"{UNIT}.timer").write_text(f"""[Unit]
Description=Daily Antigravity IDE update check
Documentation=https://github.com/EtHeGeM/antigravity-ide-updater

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
""", encoding="utf-8")

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], check=True)
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to enable systemd timer: %s", exc)
        return 1

    print(f"Timer installed.\nStatus: systemctl --user list-timers {UNIT}.timer\nLogs:   journalctl --user -u {UNIT}.service")
    return 0


def uninstall_timer() -> int:
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False)
        subprocess.run(["systemctl", "--user", "stop", f"{UNIT}.service"], check=False)
    for f in (UNIT_DIR / f"{UNIT}.service", UNIT_DIR / f"{UNIT}.timer"):
        f.unlink(missing_ok=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"Uninstalled. (State preserved: {STATE_DIR})")
    return 0


# ---------------------------------------------------------------- main
def main() -> int:
    if sys.platform != "linux":
        print("This updater is designed for Linux systems only.", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Automatic updater for Google Antigravity IDE tarball install on Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--check", action="store_true", help="Check for available updates without installing")
    parser.add_argument("--dry-run", action="store_true", help="Simulate update without modifying installation")
    parser.add_argument("--status", action="store_true", help="Show current installation and timer status")
    parser.add_argument("--install", action="store_true", help="Install scripts and enable daily systemd user timer")
    parser.add_argument("--uninstall", action="store_true", help="Disable systemd user timer and remove scripts")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[antigravity-updater] [%(levelname)s] %(message)s",
    )

    if args.status:
        return show_status()
    if args.install:
        return install_timer()
    if args.uninstall:
        return uninstall_timer()
    if args.check:
        try:
            return check_updates()
        except UpdateError as exc:
            logger.error("Update check failed: %s", exc)
            return 1
    if args.dry_run:
        try:
            return dry_run()
        except UpdateError as exc:
            logger.error("Dry run failed: %s", exc)
            return 1

    try:
        return run()
    except UpdateError as exc:
        notify("Antigravity update failed", str(exc), "critical")
        logger.error("Update failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
