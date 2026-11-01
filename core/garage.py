"""The garage: every car Hatchling has connected to, with its name, history, backups, factory file and trips."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import factory, trips, vin as vinmod
from .asbuilt import BACKUP_DIR

VIN_OK = factory.VIN_RE


@dataclass
class Car:
    vin: str
    name: str
    summary: str
    last_seen: float
    events: int
    has_factory: bool
    backups: int
    trips: int

    @property
    def backup_dir(self) -> str:
        return os.path.join(BACKUP_DIR, self.vin)


def _count_backups(v: str) -> int:
    d = os.path.join(BACKUP_DIR, v)
    return len([f for f in os.listdir(d) if f.endswith(".json")]) if os.path.isdir(d) else 0


def cars(history_rows: List[tuple], saved: Dict[str, dict]) -> List[Car]:
    """The cars the owner added to the garage (saved = settings["garage"]: {vin: {"name": ..}}), with what
    Hatchling has for each. history_rows: (vin, last seen, events) from History.vins()."""
    seen = {v: (ts or 0.0, n or 0) for v, ts, n in history_rows}
    stored = {v for v, _ in factory.stored()}
    out = []
    for v, d in (saved or {}).items():
        if not v or not VIN_OK.fullmatch(v) or not isinstance(d, dict):
            continue
        ts, n = seen.get(v, (0.0, 0))
        info = vinmod.decode(v)
        out.append(Car(v, d.get("name", ""), info.summary if info else "", max(ts, float(d.get("added", 0))), n,
                       v in stored, _count_backups(v), len(trips.load_trips(v))))
    return sorted(out, key=lambda c: -c.last_seen)


def add(saved: Dict[str, dict], vin: str, name: str = "") -> Dict[str, dict]:
    import time
    saved = dict(saved or {})
    saved[vin] = {**saved.get(vin, {}), "name": name.strip()[:40], "added": time.time()}
    return saved
