"""Problem reports: the activity log, version and adapter, with anything identifying taken out."""
from __future__ import annotations

import os
import platform
import re
from typing import List, Optional
from urllib.parse import urlencode

from .version import VERSION

VIN_ANY = re.compile(r"(?<![A-Za-z0-9])[A-HJ-NPR-Za-hj-npr-z0-9]{17}(?![A-Za-z0-9])")
USER_PATH = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s]+", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
MAX_LOG_LINES = 300
MAX_URL = 7000


def redact(text: str, vins: Optional[List[str]] = None) -> str:
    """Removes VINs (the known ones and anything shaped like one), Windows user names in paths and emails."""
    for v in vins or []:
        if v:
            text = text.replace(v, "<VIN>")
    text = VIN_ANY.sub(lambda m: "<VIN>" if any(c.isdigit() for c in m.group(0)) and
                       any(c.isalpha() for c in m.group(0)) else m.group(0), text)
    text = USER_PATH.sub(r"\1<you>", text)
    home = os.path.expanduser("~")
    if len(home) > 3:
        text = text.replace(home, "<home>")
    return EMAIL.sub("<email>", text)


def build(what: str, log_lines: List[str], adapter: str = "", car: str = "", vins: Optional[List[str]] = None) -> dict:
    log = "\n".join(log_lines[-MAX_LOG_LINES:])
    return {
        "version": VERSION,
        "windows": platform.platform(),
        "adapter": redact(adapter, vins) or "not connected",
        "car": redact(car, vins),
        "what": redact(what.strip(), vins),
        "log": redact(log, vins),
    }


def issue_link(repo: str, report: dict) -> tuple:
    """(URL, text to paste if the log didn't fit in the link, log fitted?)"""
    base = f"https://github.com/{repo}/issues/new?"
    params = {"template": "problem-report.yml", "title": f"Problem: {report['what'][:80]}" if report["what"] else "Problem: ",
              "version": report["version"], "windows": report["windows"], "adapter": report["adapter"],
              "car": report["car"], "what": report["what"]}
    full = base + urlencode({**params, "log": report["log"]})
    if len(full) <= MAX_URL:
        return full, report["log"], True
    return base + urlencode(params), report["log"], False
