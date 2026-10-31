"""DTC decoding helpers."""
from __future__ import annotations

import json
import os

from .paths import app_dir

LETTERS = "PCBU"

STATUS_BITS = [
    (0x01, "Failed now"),
    (0x02, "Failed this cycle"),
    (0x04, "Pending"),
    (0x08, "Confirmed"),
    (0x10, "Not completed since clear"),
    (0x20, "Failed since clear"),
    (0x40, "Not completed this cycle"),
    (0x80, "Warning light"),
]

_descriptions = None


def decode(raw: bytes) -> str:
    b0, b1 = raw[0], raw[1]
    code = f"{LETTERS[b0 >> 6]}{(b0 >> 4) & 3}{b0 & 0xF:X}{b1 >> 4:X}{b1 & 0xF:X}"
    if len(raw) > 2:
        code += f"-{raw[2]:02X}"
    return code


def describe_status(status: int) -> str:
    flags = [name for bit, name in STATUS_BITS if status & bit]
    return ", ".join(flags) if flags else "Stored"


def description(code: str) -> str:
    """Looks up an optional user-maintained data/dtc_descriptions.json {"P0420": "..."}."""
    global _descriptions
    if _descriptions is None:
        path = os.path.join(app_dir(), "data", "dtc_descriptions.json")
        try:
            with open(path, encoding="utf-8") as f:
                _descriptions = json.load(f)
        except (OSError, ValueError):
            _descriptions = {}
    return _descriptions.get(code.split("-")[0], "")
