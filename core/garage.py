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


def cars(history_rows: List[tuple], names: Dict[str, dict], hidden: Optional[set] = None) -> List[Car]:
    """history_rows: (vin, last seen, events) from History.vins(). Cars with only a factory file or
    backups are included too."""
    seen: Dict[str, Car] = {}
    known = [(v, ts, n) for v, ts, n in history_rows]
    for v, _ in factory.stored():
        known.append((v, 0.0, 0))
    if os.path.isdir(BACKUP_DIR):
        for d in os.listdir(BACKUP_DIR):
            known.append((d, 0.0, 0))
    stored = {v for v, _ in factory.stored()}
    for v, ts, n in known:
        if not v or not VIN_OK.fullmatch(v) or v in (hidden or set()):
            continue
        if v in seen:
            c = seen[v]
            c.last_seen, c.events = max(c.last_seen, ts or 0), c.events + (n or 0)
            continue
        info = vinmod.decode(v)
        seen[v] = Car(v, (names.get(v) or {}).get("name", ""), info.summary if info else "", ts or 0.0, n or 0,
                      v in stored, _count_backups(v), len(trips.load_trips(v)))
    return sorted(seen.values(), key=lambda c: -c.last_seen)
