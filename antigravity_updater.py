#!/usr/bin/env python3
"""Linux auto-updater for the tarball install of Antigravity IDE.

Flow: read installed version -> find latest release on the download page ->
download + extract + verify -> install (or stage if the IDE is running) ->
notify via ``notify-send``.

Environment overrides:
  ANTIGRAVITY_IDE_PATH        install directory (default ~/İndirilenler/Antigravity IDE)
  ANTIGRAVITY_UPDATER_STATE   state directory (default ~/.local/state/antigravity-ide-updater)

Usage:
  antigravity_updater.py              check once (and update if possible)
  antigravity_updater.py --install    copy scripts to ~/.local/share and enable a daily systemd user timer
  antigravity_updater.py --uninstall  disable the timer and remove installed files

Exit codes: 0 ok/up-to-date/staged, 1 error.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from antigravity_changelog import ChangelogError, _render_dom, fetch_changelog

DOWNLOAD_PAGE = "https://antigravity.google/download/linux"
LINK_RE = re.compile(
    r"https://[^\"'\s<>]+/antigravity/stable/(\d+\.\d+\.\d+)-\d+/linux-(x64|arm)/Antigravity%20IDE\.tar\.gz"
)
HOME = Path.home()
INSTALL_DIR = Path(os.environ.get("ANTIGRAVITY_IDE_PATH", HOME / "İndirilenler" / "Antigravity IDE")).expanduser()
STATE_DIR = Path(os.environ.get("ANTIGRAVITY_UPDATER_STATE", HOME / ".local/state/antigravity-ide-updater")).expanduser()
STATE_FILE = STATE_DIR / "state.json"
PENDING_DIR = STATE_DIR / "pending"
# User data that must never be touched by any move/delete.
PROTECTED = [HOME, HOME / ".config", HOME / ".antigravity", HOME / ".gemini", HOME / ".local"]


class UpdateError(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers
def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def log(msg: str) -> None:
    print(f"[antigravity-updater] {msg}", flush=True)


def notify(title: str, body: str = "", urgency: str = "normal") -> None:
    log(f"{title}: {body}" if body else title)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", urgency, "-a", "Antigravity Updater", title, body[:500]],
                       check=False)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def notify_once(state: dict, key: str, title: str, body: str = "", urgency: str = "normal") -> None:
    """Notify only if this exact key hasn't been notified before."""
    if state.get("notified", {}).get(key):
        log(f"(bildirim zaten gönderildi: {key})")
        return
    notify(title, body, urgency)
    state.setdefault("notified", {})[key] = True
    save_state(state)


def assert_safe(path: Path) -> None:
    """Refuse to move/delete anything that is or contains user data dirs."""
    p = path.resolve()
    for prot in PROTECTED:
        prot = prot.resolve()
        if p == prot or prot.is_relative_to(p):
            raise UpdateError(f"Güvenlik: korunan yol üzerinde işlem reddedildi: {p}")
    if not (p / "resources" / "app" / "product.json").exists():
        raise UpdateError(f"Güvenlik: {p} bir Antigravity IDE kurulumu gibi görünmüyor.")


# ---------------------------------------------------------------- steps
def installed_version(root: Path = INSTALL_DIR) -> str:
    pj = root / "resources" / "app" / "product.json"
    try:
        return json.loads(pj.read_text())["ideVersion"]
    except (OSError, ValueError, KeyError) as exc:
        raise UpdateError(f"Kurulu sürüm okunamadı ({pj}): {exc}") from exc


def arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm"
    raise UpdateError(f"Desteklenmeyen mimari: {m}")


def latest_release() -> tuple[str, str]:
    """Return (version, tarball_url) for this machine's architecture."""
    dom = _render_dom(DOWNLOAD_PAGE)
    want = arch()
    found = [(m.group(1), m.group(0)) for m in LINK_RE.finditer(dom) if m.group(2) == want]
    if not found:
        raise UpdateError("İndirme sayfasında IDE arşiv linki bulunamadı; sayfa yapısı değişmiş olabilir.")
    return max(found, key=lambda x: parse_version(x[0]))


def release_notes(version: str) -> str:
    try:
        return dict(fetch_changelog("ide")).get(version, "")
    except ChangelogError as exc:
        log(f"Changelog alınamadı: {exc}")
        return ""


def ide_running(root: Path = INSTALL_DIR) -> bool:
    root = root.resolve()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            exe = Path(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            continue
        if exe.is_relative_to(root):
            return True
    return False


def download_and_extract(url: str, version: str) -> Path:
    """Download + extract into PENDING_DIR/<version>; return the IDE root inside it."""
    target = PENDING_DIR / version
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    archive = target / "ide.tar.gz"
    log(f"İndiriliyor: {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(archive, "wb") as fh:
            shutil.copyfileobj(resp, fh, 1 << 20)
        with tarfile.open(archive) as tar:
            tar.extractall(target / "x", filter="tar")
    except (OSError, tarfile.TarError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise UpdateError(f"İndirme/açma başarısız: {exc}") from exc
    archive.unlink()
    return verify_pending(version)


def verify_pending(version: str) -> Path:
    base = PENDING_DIR / version / "x"
    roots = [p.parents[2] for p in base.glob("**/resources/app/product.json")]
    if len(roots) != 1:
        raise UpdateError(f"Arşivde IDE kökü bulunamadı/belirsiz ({len(roots)}).")
    got = installed_version(roots[0])
    if got != version:
        raise UpdateError(f"Arşiv sürümü uyuşmuyor: beklenen {version}, bulunan {got}.")
    return roots[0]


def install(new_root: Path, version: str) -> None:
    assert_safe(INSTALL_DIR)
    backup = INSTALL_DIR.with_name(INSTALL_DIR.name + ".bak")
    if backup.exists():
        assert_safe(backup)
        shutil.rmtree(backup)
    os.rename(INSTALL_DIR, backup)  # same filesystem -> atomic
    try:
        shutil.move(str(new_root), str(INSTALL_DIR))
        got = installed_version(INSTALL_DIR)
        if got != version:
            raise UpdateError(f"Kurulum sonrası sürüm {got}, beklenen {version}.")
    except Exception as exc:
        if INSTALL_DIR.exists():
            shutil.rmtree(INSTALL_DIR, ignore_errors=True)
        os.rename(backup, INSTALL_DIR)
        raise UpdateError(f"Kurulum başarısız, yedek geri yüklendi: {exc}") from exc
    shutil.rmtree(PENDING_DIR / version, ignore_errors=True)


# ---------------------------------------------------------------- main
def run() -> int:
    state = load_state()
    current = installed_version()
    log(f"Kurulum: {INSTALL_DIR} (v{current})")

    try:
        latest, url = latest_release()
    except (UpdateError, ChangelogError) as exc:
        notify("Antigravity güncelleme kontrolü başarısız", str(exc), "critical")
        return 1
    log(f"En son sürüm: v{latest}")

    if parse_version(latest) <= parse_version(current):
        log("Güncel.")
        shutil.rmtree(PENDING_DIR, ignore_errors=True)
        return 0

    notes = release_notes(latest)
    notify_once(state, f"found:{latest}", f"Antigravity IDE v{latest} mevcut",
                f"Kurulu: v{current}\n\n{notes}")

    # Reuse a previously staged download if it is valid.
    try:
        new_root = verify_pending(latest)
        log("Önceden indirilmiş paket kullanılıyor.")
    except UpdateError:
        new_root = download_and_extract(url, latest)

    if ide_running():
        notify_once(state, f"staged:{latest}", f"Antigravity IDE v{latest} hazır",
                    "IDE kapandıktan sonraki kontrolde kurulacak.")
        return 0

    install(new_root, latest)
    notify(f"Antigravity IDE v{latest} kuruldu", f"v{current} → v{latest}\nYedek: {INSTALL_DIR.name}.bak")
    state["installed"] = latest
    save_state(state)
    return 0


# ---------------------------------------------------------------- systemd
UNIT = "antigravity-ide-updater"
APP_DIR = HOME / ".local/share" / UNIT
UNIT_DIR = HOME / ".config/systemd/user"


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def install_timer() -> int:
    if not shutil.which("systemctl"):
        print("systemctl bulunamadı.", file=sys.stderr)
        return 1
    src = Path(__file__).resolve().parent
    APP_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("antigravity_updater.py", "antigravity_changelog.py"):
        shutil.copy2(src / name, APP_DIR / name)

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / f"{UNIT}.service").write_text(f"""[Unit]
Description=Antigravity IDE updater
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment="ANTIGRAVITY_IDE_PATH={INSTALL_DIR}"
Environment="ANTIGRAVITY_UPDATER_STATE={STATE_DIR}"
ExecStart={sys.executable} {APP_DIR / 'antigravity_updater.py'}
""")
    (UNIT_DIR / f"{UNIT}.timer").write_text(f"""[Unit]
Description=Daily Antigravity IDE update check

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
""")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", f"{UNIT}.timer")
    print(f"Kuruldu. Durum: systemctl --user list-timers {UNIT}.timer\n"
          f"Log: journalctl --user -u {UNIT}.service")
    return 0


def uninstall_timer() -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False)
    for f in (UNIT_DIR / f"{UNIT}.service", UNIT_DIR / f"{UNIT}.timer"):
        f.unlink(missing_ok=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("Kaldırıldı. (Durum klasörü korunuyor: " + str(STATE_DIR) + ")")
    return 0


def main() -> int:
    if sys.platform != "linux":
        print("Bu güncelleyici yalnızca Linux içindir.", file=sys.stderr)
        return 1
    if "--install" in sys.argv:
        return install_timer()
    if "--uninstall" in sys.argv:
        return uninstall_timer()
    try:
        return run()
    except UpdateError as exc:
        notify("Antigravity güncellemesi başarısız", str(exc), "critical")
        return 1


if __name__ == "__main__":
    sys.exit(main())
