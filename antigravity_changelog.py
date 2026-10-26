#!/usr/bin/env python3
"""Fetch the Google Antigravity changelog for a given product tab.

The changelog page renders its content with JavaScript, so a headless Chrome
is used to obtain the final DOM. All product tabs live in the same HTML under
``id="panel-<tab>"`` (ide, cli, hub, sdk, ...).
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys

CHANGELOG_URL = "https://antigravity.google/docs/changelog/?tab={tab}"
CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


class ChangelogError(RuntimeError):
    """Raised when the changelog cannot be fetched or parsed."""


def _render_dom(url: str, timeout: int = 60) -> str:
    chrome = next((c for c in CHROME_CANDIDATES if shutil.which(c)), None)
    if not chrome:
        raise ChangelogError("Chrome or Chromium executable not found on the system.")
    try:
        result = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--virtual-time-budget=10000",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ChangelogError(f"Failed to render page DOM: {exc}") from exc
    return result.stdout


def _extract_panel(dom: str, tab: str) -> str:
    start = re.search(rf'id="panel-{re.escape(tab)}"', dom)
    if not start:
        raise ChangelogError(f"'panel-{tab}' not found in DOM; page structure may have changed.")
    rest = dom[start.end():]
    nxt = re.search(r'id="panel-[\w-]+"', rest)
    return rest[: nxt.start()] if nxt else rest


def _to_lines(fragment: str) -> list[str]:
    fragment = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", fragment))
    return [line.strip() for line in text.splitlines() if line.strip()]


def fetch_changelog(tab: str = "ide") -> list[tuple[str, str]]:
    """Return ``[(version, notes), ...]`` newest first, without duplicates."""
    lines = _to_lines(_extract_panel(_render_dom(CHANGELOG_URL.format(tab=tab)), tab))

    entries: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for line in lines:
        match = VERSION_RE.match(line)
        if match:
            version = match.group(1)
            if version in seen:
                entries.append(("", []))  # duplicate header: discard its notes
            else:
                seen.add(version)
                entries.append((version, []))
        elif entries:
            entries[-1][1].append(line)

    result = [(v, "\n".join(n)) for v, n in entries if v]
    if not result:
        raise ChangelogError(f"No version entries found in changelog panel for '{tab}'.")
    return result


def latest_version(tab: str = "ide") -> str:
    return fetch_changelog(tab)[0][0]


if __name__ == "__main__":
    tab = sys.argv[1] if len(sys.argv) > 1 else "ide"
    try:
        entries = fetch_changelog(tab)
    except ChangelogError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    version, notes = entries[0]
    print(f"Latest {tab} version: v{version}\n\n{notes}")
