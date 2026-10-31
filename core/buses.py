"""Diagnostic buses a switched ELM327 can reach on Ford-family vehicles.

The adapter's switch moves the CAN lines between OBD pins 6/14 (HS position) and
pins 3/11 (MS position). Different Ford generations use pins 3/11 for different
things, so a bus here is: which switch position + which speed.
"""
from dataclasses import dataclass
from typing import Dict, Optional

HS = "HS"
MS = "MS"
HS2 = "HS2"
OBD = "OBD"


@dataclass(frozen=True)
class Bus:
    key: str
    label: str
    pins: str
    kbps: int            # 0 = let the adapter pick (generic OBD)
    switch: Optional[str]  # adapter switch position, "HS" or "MS"
    protocol: str        # "can" = Ford module diagnostics, "obd" = generic OBD-II only


BUSES: Dict[str, Bus] = {
    HS: Bus(HS, "HS-CAN", "6/14", 500, "HS", "can"),
    MS: Bus(MS, "MS-CAN", "3/11", 125, "MS", "can"),
    HS2: Bus(HS2, "HS-CAN on pins 3/11", "3/11", 500, "MS", "can"),
    OBD: Bus(OBD, "OBD-II (any protocol)", "standard", 0, "HS", "obd"),
}


def label(key: str) -> str:
    return BUSES[key].label if key in BUSES else key
