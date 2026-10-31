"""The shared feature library, hosted in a GitHub repository.

A copy of the library ships inside Hatchling (works offline). When online, Hatchling
downloads the latest index from GitHub and caches it. Sharing a feature or confirming one
opens a GitHub issue that Hatchling has already filled in; a GitHub Action adds approved
features and counts confirmations (see .github/workflows/library.yml).

Nothing is ever sent automatically - the user sees and submits every issue themselves -
and feature files never contain a VIN.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from .paths import app_dir, user_dir
from .version import UPDATE_REPO, VERSION

# The repository holding library/ - defaults to the same repo as app updates.
LIBRARY_REPO = "HackTheScript001/Hatchling"
BRANCH = "main"
RAW_BASE = "https://raw.githubusercontent.com"
SETTINGS = user_dir("settings.json")
CACHE = user_dir("library-cache")
BUNDLED = os.path.join(app_dir(), "library")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
MAX_URL = 7000   # GitHub rejects very long URLs; longer features go via the clipboard


class LibraryError(Exception):
    pass


# ---- settings ------------------------------------------------------------------------
def settings() -> dict:
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not d.get("install_id"):
        d["install_id"] = uuid.uuid4().hex
        save_settings(d)
    return d


def save_settings(d: dict):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def repo() -> str:
    r = (settings().get("library_repo") or LIBRARY_REPO or settings().get("update_repo") or UPDATE_REPO or "").strip()
    return r if REPO_RE.match(r) else ""


def check_repo(r: str) -> str:
    r = r.strip().removeprefix("https://github.com/").strip("/")
    if r.endswith(".git"):
        r = r[:-4]
    if not REPO_RE.match(r):
        raise LibraryError("Enter the GitHub repository as owner/name, e.g. liammayes/hatchling")
    return r


# ---- reading the library ----------------------------------------------------------------
def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fetch(url: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"Hatchling/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(4 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as e:
        raise LibraryError("The library isn't published yet (404)" if e.code == 404 else
                           f"GitHub answered with error {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LibraryError(f"Couldn't reach GitHub ({getattr(e, 'reason', e)})")
    if len(raw) > 4 * 1024 * 1024:
        raise LibraryError("The library file is unexpectedly large")
    return raw


def _raw_url(path: str, base: Optional[str] = None) -> str:
    r = repo()
    if not r:
        raise LibraryError("No library repository set")
    return f"{base or RAW_BASE}/{r}/{BRANCH}/library/{path}"


def _valid_index(d) -> bool:
    return isinstance(d, dict) and d.get("schema") == 1 and isinstance(d.get("features"), list)


def bundled_index() -> Optional[dict]:
    d = _read_json(os.path.join(BUNDLED, "index.json"))
    return d if _valid_index(d) else None


def cached_index() -> Optional[dict]:
    d = _read_json(os.path.join(CACHE, "index.json"))
    return d if _valid_index(d) else None


def best_index() -> Tuple[Optional[dict], str]:
    """Newest index available offline: the last download or the copy inside the app."""
    options = [(d, src) for d, src in ((cached_index(), "downloaded"), (bundled_index(), "built in")) if d]
    if not options:
        return None, ""
    return max(options, key=lambda x: x[0].get("generated", ""))


def update(base: Optional[str] = None) -> dict:
    """Downloads the latest index from GitHub and caches it."""
    raw = _fetch(_raw_url("index.json", base))
    try:
        d = json.loads(raw)
    except ValueError:
        raise LibraryError("GitHub sent an index that isn't valid")
    if not _valid_index(d):
        raise LibraryError("GitHub sent an index Hatchling doesn't understand - update Hatchling")
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, "index.json"), "wb") as f:
        f.write(raw)
    return d


def _safe_path(p: str) -> str:
    if not isinstance(p, str) or not re.match(r"^features/[a-z]+/[a-z0-9-]+\.json$", p):
        raise LibraryError("The library index has a bad file path")
    return p


def get(entry: dict, base: Optional[str] = None) -> dict:
    """Full feature file: from the copy inside the app if it's there, otherwise from GitHub."""
    path = _safe_path(entry.get("path", ""))
    local = _read_json(os.path.join(BUNDLED, *path.split("/")))
    if isinstance(local, dict) and local.get("library_id") == entry["id"]:
        return local
    try:
        d = json.loads(_fetch(_raw_url(path, base)))
    except ValueError:
        raise LibraryError("GitHub sent a feature file that isn't valid")
    if not isinstance(d, dict) or d.get("library_id") != entry["id"]:
        raise LibraryError("The feature file doesn't match the library index")
    return d


# ---- installing ------------------------------------------------------------------------
def installed(folder: Optional[str] = None) -> Dict[str, str]:
    """library id -> local file path, for features already installed."""
    folder = folder or user_dir("features")
    out = {}
    if not os.path.isdir(folder):
        return out
    for fn in os.listdir(folder):
        if fn.endswith(".json"):
            p = os.path.join(folder, fn)
            d = _read_json(p)
            if isinstance(d, dict) and d.get("library_id"):
                out[d["library_id"]] = p
    return out


def install(data: dict, folder: Optional[str] = None) -> str:
    """Saves a library feature into your features folder (replacing an older copy of the same one).
    It's marked not verified until you've tested it on your car."""
    from .features import _parse
    from .finder import save_feature
    lid = data.get("library_id")
    if not lid:
        raise LibraryError("That feature has no library id")
    feat = _parse(data, lid)
    if not feat.parts or not feat.configured:
        raise LibraryError("That feature is incomplete")
    d = dict(data)
    d["id"] = lid
    d["verified"] = False
    d["imported_from"] = "Hatchling library"
    existing = installed(folder).get(lid)
    if existing:
        with open(existing, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        return existing
    return save_feature(d, folder)


# ---- sharing via GitHub issues ----------------------------------------------------------
SHARE_KEYS = ("name", "category", "description", "vehicle", "learned_on", "hardware_required", "source",
              "hardware_checks", "modules")


def shareable(feature: dict) -> dict:
    """Only the fields the library keeps (no local paths, import notes or verified flags)."""
    return {k: feature[k] for k in SHARE_KEYS if k in feature}


def share_link(feature: dict, make: str = "Ford") -> Tuple[str, str, bool]:
    """(issue URL, feature JSON, prefilled?) - if the JSON is too long for a link, the caller
    should put it on the clipboard for the user to paste."""
    r = repo()
    if not r:
        raise LibraryError("No library repository set")
    text = json.dumps(shareable(feature), indent=2)
    base = f"https://github.com/{r}/issues/new?"
    params = {"template": "share-feature.yml", "title": f"Feature: {feature.get('name', '')}"[:120]}
    full = base + urlencode({**params, "feature": text})
    if len(full) <= MAX_URL:
        return full, text, True
    return base + urlencode(params), text, False


def confirm_link(library_id: str, part: str = "") -> str:
    r = repo()
    if not r:
        raise LibraryError("No library repository set")
    return f"https://github.com/{r}/issues/new?" + urlencode(
        {"template": "confirm-feature.yml", "title": f"Works on my car: {library_id}",
         "feature": library_id, "part": part})


def entry_matches_make(entry: dict, make: Optional[str]) -> bool:
    return not make or entry.get("make") in (make, "Other")


def factory_share_link(anon: dict) -> Tuple[str, str, bool]:
    """(issue URL, file JSON, prefilled?) for the 'Share a factory file' form. Factory files are usually
    too long for a link, so the caller copies the JSON to the clipboard for pasting."""
    r = repo()
    if not r:
        raise LibraryError("No library repository set")
    text = json.dumps(anon, indent=1)
    base = f"https://github.com/{r}/issues/new?"
    title = f"Factory file: {anon.get('year') or ''} {anon.get('make', '')} {anon.get('model', '')}".strip()
    params = {"template": "share-factory.yml", "title": title[:120]}
    full = base + urlencode({**params, "factory": text})
    if len(full) <= MAX_URL:
        return full, text, True
    return base + urlencode(params), text, False
