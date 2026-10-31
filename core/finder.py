"""Feature finder: compare two configurations bit by bit and turn the difference into a feature file.

Typical use: read a module, change a setting with another tool or copy a backup from a car
that has the feature, then compare. The differing bits are what the feature uses.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .asbuilt import Blocks
from .paths import user_dir


@dataclass
class BitChange:
    did: int
    byte: int
    bit: int
    before: int
    after: int


@dataclass
class ByteChange:
    did: int
    byte: int
    before: int
    after: int

    @property
    def mask(self) -> int:
        return self.before ^ self.after

    @property
    def bits(self) -> List[int]:
        return [b for b in range(8) if self.mask >> b & 1]


def compare(before: Blocks, after: Blocks) -> List[ByteChange]:
    """Changed bytes in blocks both sides have. Blocks of different length are compared up to the shorter."""
    out = []
    for did in sorted(set(before) & set(after)):
        for i, (a, b) in enumerate(zip(before[did], after[did])):
            if a != b:
                out.append(ByteChange(did, i, a, b))
    return out


def bit_changes(changes: List[ByteChange]) -> List[BitChange]:
    return [BitChange(c.did, c.byte, b, c.before >> b & 1, c.after >> b & 1) for c in changes for b in c.bits]


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "feature"


def _module_entry(bus: str, module: int, label: str, changes: List[ByteChange],
                  identity: Optional[dict] = None) -> dict:
    entry = {"module": f"0x{module:03X}", "bus": bus, "label": label,
             "changes": [{"did": f"{c.did:04X}", "byte": c.byte, "mask": f"0x{c.mask:02X}",
                          "on": f"0x{c.after & c.mask:02X}", "off": f"0x{c.before & c.mask:02X}"} for c in changes]}
    if identity:
        if identity.get("Part number"):
            entry["part"] = identity["Part number"]
        if identity.get("Software"):
            entry["software"] = identity["Software"]
    return entry


def make_feature(name: str, bus: str, module: int, module_label: str, changes: List[ByteChange],
                 description: str = "", category: str = "Custom", hardware: str = "",
                 vehicle: str = "", source: str = "Found with Hatchling's feature finder",
                 identity: Optional[dict] = None, learned_on: str = "") -> dict:
    """Feature definition where 'on' is the after-state and 'off' the before-state, masked to changed bits."""
    return {
        "id": slug(name), "name": name, "category": category, "description": description,
        "vehicle": vehicle, "learned_on": learned_on or vehicle, "verified": False, "source": source,
        "hardware_required": hardware, "hardware_checks": [],
        "modules": [_module_entry(bus, module, module_label, changes, identity)],
    }


def save_feature(feature: dict, folder: Optional[str] = None) -> str:
    folder = folder or user_dir("features")
    os.makedirs(folder, exist_ok=True)
    base = os.path.join(folder, slug(feature["name"]))
    path, n = base + ".json", 2
    while os.path.exists(path):
        path, n = f"{base}-{n}.json", n + 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feature, f, indent=2)
    return path


def merge_into(path: str, bus: str, module: int, module_label: str, changes: List[ByteChange],
               identity: Optional[dict] = None):
    """Adds another module's changes to an existing feature file (features often span modules)."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    part = _module_entry(bus, module, module_label, changes, identity)
    d.setdefault("modules", [])
    d["modules"] = [m for m in d["modules"] if not (m.get("bus") == bus and int(m["module"], 0) == module)]
    d["modules"].append(part)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


# ---- sharing ---------------------------------------------------------------------
def set_verified(path: str, verified: bool = True):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    d["verified"] = verified
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def export_feature(src: str, dest: str):
    """Writes a shareable copy (no local paths; a full VIN is never stored in feature files)."""
    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    d.pop("imported_from", None)
    d["shared_by_hatchling"] = True
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def import_feature(src: str, folder: Optional[str] = None) -> str:
    """Copies a shared feature into your features folder. It's marked not verified for your car
    until you test it, whatever it was on the car it came from."""
    from .features import _parse  # validate before saving
    if os.path.getsize(src) > 1024 * 1024:
        raise ValueError("That file is too big to be a feature file")
    with open(src, encoding="utf-8") as f:
        d = json.load(f)
    feat = _parse(d, slug(os.path.splitext(os.path.basename(src))[0]))
    if not feat.parts:
        raise ValueError("The file has no modules in it")
    d["verified_on_source_car"] = bool(d.get("verified", False))
    d["verified"] = False
    d["imported_from"] = os.path.basename(src)
    d.pop("shared_by_hatchling", None)
    d.setdefault("id", feat.id)
    return save_feature(d, folder)
