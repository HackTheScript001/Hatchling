"""Update check against a GitHub repository's latest release.

Set the repository in core/version.py (UPDATE_REPO = "owner/name") or in
Documents\\Hatchling\\settings.json as {"update_repo": "owner/name"}. Nothing is sent
except a normal request for the public release page.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from .paths import user_dir
from .version import UPDATE_REPO, VERSION


@dataclass
class Release:
    version: str
    url: str
    notes: str


def parse_version(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote: str, local: str = VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def configured_repo() -> str:
    try:
        with open(user_dir("settings.json"), encoding="utf-8") as f:
            return json.load(f).get("update_repo", "") or UPDATE_REPO
    except (OSError, ValueError):
        return UPDATE_REPO


def check(timeout: float = 6.0) -> Optional[Release]:
    """Returns the newer release, or None if up to date / not configured. Raises on network errors."""
    repo = configured_repo()
    if not repo or "/" not in repo:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "Hatchling"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    if not is_newer(tag):
        return None
    installer = next((a["browser_download_url"] for a in data.get("assets", [])
                      if a.get("name", "").lower().endswith(".exe")), data.get("html_url", ""))
    return Release(tag.lstrip("v"), installer, (data.get("body") or "").strip()[:600])
