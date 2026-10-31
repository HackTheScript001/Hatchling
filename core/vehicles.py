"""Vehicle profiles: which buses to use for a family of models, matched from the VIN."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List, Optional

from .buses import BUSES
from .paths import definition_files, user_dir
from .vin import VinInfo

VEHICLES_DIR = user_dir("vehicles")


@dataclass
class Profile:
    id: str
    make: str
    name: str
    models: str
    years: tuple
    buses: List[str]
    makes: List[str]
    notes: str
    priority: int = 0   # tie-break when the VIN has no model year (higher = more common)

    @property
    def obd_only(self) -> bool:
        return all(BUSES[b].protocol == "obd" for b in self.buses)

    @property
    def can_buses(self) -> List[str]:
        return [b for b in self.buses if BUSES[b].protocol == "can"]


def load_all(folder: Optional[str] = None) -> List[Profile]:
    out = []
    files = definition_files("vehicles") if folder is None else [
        (fn, os.path.join(folder, fn)) for fn in sorted(os.listdir(folder))
        if fn.endswith(".json") and not fn.startswith("_")] if os.path.isdir(folder) else []
    for fn, path in files:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            buses = [b.upper() for b in d["buses"]]
            unknown = [b for b in buses if b not in BUSES]
            if unknown:
                raise ValueError(f"unknown bus {unknown}")
            out.append(Profile(d.get("id", fn[:-5]), d.get("make", ""), d["name"], d.get("models", ""),
                               tuple(d.get("years", [1996, 2100])), buses, d.get("matches_makes", []),
                               d.get("notes", ""), int(d.get("priority", 0))))
        except (OSError, ValueError, KeyError) as e:
            print(f"Skipping vehicle profile {fn}: {e}")
    return out


def suggest(profiles: List[Profile], info: Optional[VinInfo], has_can: bool = True) -> List[Profile]:
    """Profiles ranked best-first for this VIN.

    - make match first; a make no profile knows goes to the generic OBD-II profile
    - with a model year: the narrowest year range that contains it (most specific)
    - without a year: the most common layout for that make (priority)
    """
    known_make = bool(info) and any(info.make in p.makes for p in profiles)

    def score(p: Profile):
        s = 0
        if known_make and info.make in p.makes:
            s += 10
        if not p.makes:
            s += 0 if known_make else 20   # unknown make -> generic profile
        if info and info.year and p.years[0] <= info.year <= p.years[1]:
            s += 5
        if not has_can and p.obd_only:
            s += 8
        if has_can and not p.obd_only:
            s += 3
        in_range = bool(info and info.year and p.years[0] <= info.year <= p.years[1])
        span = (p.years[1] - p.years[0]) if in_range else 999
        return (-s, span, -p.priority)
    return sorted(profiles, key=score)
