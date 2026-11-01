"""Fuel economy, trips and the performance timer - all worked out from standard engine data.

Fuel use comes from the engine's fuel rate (PID 5E) when it has one, otherwise from the mass air
flow (PID 10) on petrol engines (air / 14.7 = fuel). Diesels without PID 5E can't be measured this
way. Figures are estimates - typically within 5-10% of the pump.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from .paths import user_dir

PETROL_AFR = 14.7          # air:fuel by mass at stoichiometric
PETROL_DENSITY = 745.0     # g per litre
DIESEL_DENSITY = 832.0
KM_PER_MILE = 1.609344
L_PER_UK_GALLON = 4.54609
TRIPS_DIR = user_dir("trips")


def fuel_lph(maf_gs: Optional[float] = None, fuel_rate_lph: Optional[float] = None, diesel: bool = False) -> Optional[float]:
    if fuel_rate_lph is not None:
        return fuel_rate_lph
    if maf_gs is None or diesel:
        return None
    return maf_gs * 3600 / (PETROL_AFR * PETROL_DENSITY)


def mpg(km: float, litres: float) -> Optional[float]:
    """UK miles per gallon."""
    if litres <= 0.0005 or km <= 0:
        return None
    return (km / KM_PER_MILE) / (litres / L_PER_UK_GALLON)


def l_per_100km(km: float, litres: float) -> Optional[float]:
    return litres / km * 100 if km > 0.01 else None


@dataclass
class Trip:
    started: float = field(default_factory=time.time)
    seconds: float = 0.0
    km: float = 0.0
    litres: float = 0.0
    idle_seconds: float = 0.0
    top_kmh: float = 0.0
    fuel_known: bool = True
    _last: Optional[float] = None

    def add(self, now: float, speed_kmh: Optional[float], lph: Optional[float]):
        """One reading. Integrates distance and fuel since the previous reading (gaps over 5 s are skipped)."""
        if self._last is not None:
            dt = now - self._last
            if 0 < dt <= 5:
                self.seconds += dt
                if speed_kmh is not None:
                    self.km += speed_kmh * dt / 3600
                    if speed_kmh < 1:
                        self.idle_seconds += dt
                if lph is None:
                    self.fuel_known = False
                else:
                    self.litres += lph * dt / 3600
        if speed_kmh is not None:
            self.top_kmh = max(self.top_kmh, speed_kmh)
        self._last = now

    @property
    def miles(self) -> float:
        return self.km / KM_PER_MILE

    @property
    def mpg(self) -> Optional[float]:
        return mpg(self.km, self.litres) if self.fuel_known else None

    def summary(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["mpg"] = self.mpg
        return d


def save_trip(vin: str, trip: Trip) -> str:
    os.makedirs(TRIPS_DIR, exist_ok=True)
    path = os.path.join(TRIPS_DIR, f"{vin or 'unknown'}.json")
    trips = load_trips(vin)
    trips.insert(0, trip.summary())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trips[:500], f, indent=1)
    return path


def load_trips(vin: str) -> List[dict]:
    try:
        with open(os.path.join(TRIPS_DIR, f"{vin or 'unknown'}.json"), encoding="utf-8") as f:
            d = json.load(f)
        return [t for t in d if isinstance(t, dict)] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


# ---- performance timer --------------------------------------------------------------
RUNS = {"0-60 mph": (0.0, 60 * KM_PER_MILE), "30-70 mph": (30 * KM_PER_MILE, 70 * KM_PER_MILE),
        "0-100 km/h": (0.0, 100.0)}


class PerfRun:
    """Times a run between two speeds from a stream of (time, speed) readings. Crossing times are
    interpolated between readings, so a 5-10 readings-a-second feed gives about a tenth of a second."""

    def __init__(self, name: str):
        self.name = name
        self.lo, self.hi = RUNS[name]
        self.start: Optional[float] = None
        self.result: Optional[float] = None
        self.armed = self.lo > 0      # rolling runs are armed from the start; 0-x needs the car stopped
        self.prev: Optional[tuple] = None
        self.state = "Get to below the start speed" if self.lo > 0 else "Stop the car to start"

    def add(self, t: float, kmh: float) -> Optional[float]:
        if self.result is not None:
            return self.result
        p = self.prev
        self.prev = (t, kmh)
        if self.lo == 0:
            if kmh < 0.5:
                self.armed, self.start = True, None
                self.state = "Ready - go when you're ready"
                return None
            if self.armed and self.start is None and p is not None:
                self.start = p[0]                     # last reading at standstill
                self.state = "Running..."
        else:
            if kmh < self.lo - 3:
                self.armed, self.start = True, None
                self.state = f"Ready - accelerate through {self.lo / KM_PER_MILE:.0f} mph"
            elif self.armed and self.start is None and p is not None and p[1] < self.lo <= kmh:
                self.start = _cross(p, (t, kmh), self.lo)
                self.state = "Running..."
        if self.start is not None and p is not None and p[1] < self.hi <= kmh:
            self.result = round(_cross(p, (t, kmh), self.hi) - self.start, 2)
            self.state = f"{self.result:.2f} s"
        if self.start is not None and p is not None and kmh < p[1] - 5:
            self.start, self.armed = None, False      # lifted off - run abandoned
            self.state = "Run abandoned - slow down to try again"
        return self.result


def _cross(a: tuple, b: tuple, v: float) -> float:
    (t1, v1), (t2, v2) = a, b
    return t1 if v2 == v1 else t1 + (v - v1) / (v2 - v1) * (t2 - t1)
