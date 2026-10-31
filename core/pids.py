"""Standard OBD-II mode 01 PIDs for live data."""
from dataclasses import dataclass
from typing import Callable


@dataclass
class Pid:
    pid: int
    name: str
    unit: str
    formula: Callable[[bytes], float]
    lo: float
    hi: float


PIDS = [
    Pid(0x0C, "Engine speed", "rpm", lambda d: (d[0] * 256 + d[1]) / 4, 0, 7000),
    Pid(0x0D, "Vehicle speed", "km/h", lambda d: d[0], 0, 200),
    Pid(0x05, "Coolant temp", "°C", lambda d: d[0] - 40, -20, 120),
    Pid(0x0F, "Intake air temp", "°C", lambda d: d[0] - 40, -20, 80),
    Pid(0x04, "Engine load", "%", lambda d: d[0] * 100 / 255, 0, 100),
    Pid(0x11, "Throttle position", "%", lambda d: d[0] * 100 / 255, 0, 100),
    Pid(0x0B, "Manifold pressure", "kPa", lambda d: d[0], 0, 255),
    Pid(0x10, "Mass air flow", "g/s", lambda d: (d[0] * 256 + d[1]) / 100, 0, 200),
    Pid(0x42, "Module voltage", "V", lambda d: (d[0] * 256 + d[1]) / 1000, 10, 16),
    Pid(0x2F, "Fuel level", "%", lambda d: d[0] * 100 / 255, 0, 100),
]
