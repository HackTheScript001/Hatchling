"""Small online helpers: VIN details from NHTSA, the community feed from GitHub, and spotting
a downloaded factory file. Everything here is optional - Hatchling works offline without it."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import quote

from .paths import user_dir
from .version import VERSION

VPIC = "https://vpic.nhtsa.dot.gov/api"
GITHUB_API = "https://api.github.com"
# Ford's As-Built lookup page (owners sign in with their own free account). Can be changed in settings.json
# as "factory_url" if Ford moves it.
FACTORY_URL = "https://www.motorcraftservice.com/AsBuilt"
FACTORY_EXTS = (".ab", ".xml", ".txt", ".htm", ".html")


class OnlineError(Exception):
    pass


def _get_json(url: str, timeout: float = 10.0, headers: Optional[dict] = None):
    req = urllib.request.Request(url, headers={"User-Agent": f"Hatchling/{VERSION}", "Accept": "application/json",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as e:
        raise OnlineError(f"the server answered with error {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OnlineError(f"couldn't reach it ({getattr(e, 'reason', e)})")
    if len(raw) > 2 * 1024 * 1024:
        raise OnlineError("unexpectedly large reply")
    try:
        return json.loads(raw)
    except ValueError:
        raise OnlineError("the reply wasn't valid data")


# ---- VIN details (NHTSA vPIC - free US government VIN database) --------------------------
VPIC_FIELDS = {"Make": "Make", "Model": "Model", "ModelYear": "Model year", "Series": "Series", "Trim": "Trim",
               "BodyClass": "Body", "DisplacementL": "Engine size (L)", "EngineModel": "Engine",
               "FuelTypePrimary": "Fuel", "TransmissionStyle": "Gearbox", "PlantCity": "Built in",
               "PlantCountry": "Country built"}


def vin_details(vin: str, year: Optional[int] = None, base: str = VPIC) -> Dict[str, str]:
    """Readable details for a VIN, cached per VIN. Strongest for North American cars; European VINs often
    come back with only some fields filled in."""
    vin = (vin or "").strip().upper()
    if len(vin) != 17 or not vin.isalnum():
        raise OnlineError("that isn't a full VIN")
    cache = user_dir("vin-cache", f"{vin}.json")
    try:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    q = f"?format=json" + (f"&modelyear={int(year)}" if year else "")
    d = _get_json(f"{base}/vehicles/DecodeVinValues/{quote(vin)}{q}")
    rows = d.get("Results") if isinstance(d, dict) else None
    if not rows or not isinstance(rows[0], dict):
        raise OnlineError("no details for this VIN")
    r = rows[0]
    out = {label: str(r.get(key) or "").strip() for key, label in VPIC_FIELDS.items()}
    out = {k: v for k, v in out.items() if v and v.lower() not in ("not applicable", "0", "0.0")}
    if out.get("Engine size (L)"):
        try:
            out["Engine size (L)"] = f"{float(out['Engine size (L)']):.1f}"
        except ValueError:
            pass
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


# ---- community feed (GitHub issues: feedback, shared features, factory files) ------------
def community_posts(repo: str, base: str = GITHUB_API, limit: int = 30) -> List[dict]:
    """Latest issues on the repo (no account needed). Pull requests are left out."""
    if not repo:
        raise OnlineError("no repository set")
    items = _get_json(f"{base}/repos/{repo}/issues?state=all&sort=updated&direction=desc&per_page={int(limit)}",
                      headers={"Accept": "application/vnd.github+json"})
    if not isinstance(items, list):
        raise OnlineError("unexpected reply from GitHub")
    out = []
    for it in items:
        if not isinstance(it, dict) or "pull_request" in it:
            continue
        out.append({
            "title": str(it.get("title", ""))[:200],
            "url": str(it.get("html_url", "")),
            "state": it.get("state", ""),
            "labels": [str(l.get("name", "")) for l in it.get("labels", []) if isinstance(l, dict)],
            "comments": int(it.get("comments", 0) or 0),
            "updated": str(it.get("updated_at", ""))[:10],
            "by": str((it.get("user") or {}).get("login", "")),
        })
    return out


def post_kind(labels: List[str]) -> str:
    for label, name in (("feedback", "Feedback"), ("feature-submission", "Shared feature"),
                        ("factory-file", "Factory file"), ("confirmation", "Works on my car")):
        if label in labels:
            return name
    return "Post"


# ---- spotting a downloaded factory file ------------------------------------------------
def downloads_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


def new_factory_files(folder: str, since: float) -> List[str]:
    """Files that could be a factory As-Built download, newest first, changed after `since`."""
    if not os.path.isdir(folder):
        return []
    found = []
    for fn in os.listdir(folder):
        if not fn.lower().endswith(FACTORY_EXTS):
            continue
        p = os.path.join(folder, fn)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mtime >= since - 1 and 0 < st.st_size <= 5 * 1024 * 1024:
            found.append((st.st_mtime, p))
    return [p for _, p in sorted(found, reverse=True)]
