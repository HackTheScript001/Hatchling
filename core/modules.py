"""Module discovery. Addresses below are common Ford layouts; the scan confirms what
actually answers on your car, so treat names as best guesses until identified."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .buses import HS, HS2, MS
from .uds import UDS, UdsError

# Typical Ford-family module addresses (request IDs). The same address can sit on a different
# bus depending on model and year - the scan finds where it actually is.
NAMES = {
    0x7E0: "PCM - Powertrain", 0x7E1: "TCM - Transmission", 0x7E2: "HPCM/SOBDM - Hybrid/secondary",
    0x7E4: "BECM - Battery energy", 0x760: "ABS - Brakes", 0x737: "RCM - Airbags", 0x730: "PSCM - Steering",
    0x726: "BCM - Body", 0x720: "IPC - Instrument cluster", 0x727: "ACM - Audio", 0x7D0: "APIM - SYNC",
    0x733: "HVAC - Climate", 0x736: "PAM - Parking aid", 0x740: "DDM - Driver door", 0x741: "PDM - Passenger door",
    0x724: "SCCM - Steering column", 0x706: "IPMA - Camera", 0x765: "OCS - Seat sensor", 0x764: "CCM - Cruise radar",
    0x716: "GWM - Gateway", 0x731: "RFA - Remote/keyless", 0x744: "DSM - Driver seat", 0x751: "TRM - Trailer",
    0x7A6: "FCDIM - Front display", 0x775: "BLIS - Blind spot", 0x7C7: "HCM - Headlamp", 0x750: "SODL - Side obstacle",
    0x734: "HUD - Head-up display", 0x732: "GPSM - GPS", 0x7C4: "SASM - Shifter", 0x792: "OBCC - Charger",
}

KNOWN = {  # addresses tried by a quick scan on each bus
    HS: {a: NAMES[a] for a in (0x7E0, 0x7E1, 0x7E2, 0x7E4, 0x760, 0x737, 0x730, 0x726, 0x706, 0x765, 0x764,
                               0x716, 0x7C4, 0x724, 0x792)},
    MS: {a: NAMES[a] for a in (0x726, 0x720, 0x727, 0x7D0, 0x733, 0x736, 0x740, 0x741, 0x724, 0x731, 0x744,
                               0x751, 0x7A6, 0x775, 0x7C7, 0x734, 0x732)},
}
KNOWN[HS2] = dict(KNOWN[MS])


# Mazda's own names where they differ (same addresses on the Ford-derived Mazdas)
MAZDA_NAMES = {0x760: "DSC/ABS - Brakes", 0x737: "SAS - Airbags", 0x730: "EPS - Steering",
               0x720: "IC - Instrument cluster", 0x727: "AUDIO - Audio", 0x733: "CLIMATE - Climate"}


def name_for(bus: str, addr: int, make: str = "") -> str:
    if make == "Mazda" and addr in MAZDA_NAMES:
        return MAZDA_NAMES[addr]
    return NAMES.get(addr, f"Module {addr:03X}")


ID_DIDS = {
    0xF190: "VIN",
    0xF111: "Part number",
    0xF113: "Assembly",
    0xF188: "Software",
}


@dataclass
class Module:
    addr: int
    bus: str
    name: str
    identity: Dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.bus}:{self.addr:03X}"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.addr:03X}, {self.bus})"


def _ascii(data: bytes) -> str:
    s = data.decode("ascii", "ignore").strip("\x00 ").strip()
    return s if s.isprintable() and s else data.hex(" ").upper()


def identify(uds: UDS, m: Module):
    for did, label in ID_DIDS.items():
        try:
            m.identity[label] = _ascii(uds.read_did(m.addr, did))
        except UdsError:
            pass


def scan(uds: UDS, bus: str, full: bool = False,
         progress: Optional[Callable[[int, int], None]] = None, make: str = "") -> List[Module]:
    """Ping each address with TesterPresent. Must already be on `bus`."""
    elm = uds.elm
    addrs = list(range(0x700, 0x7F0)) if full else sorted(KNOWN.get(bus, NAMES))
    addrs = [a for a in addrs if a != 0x7DF]
    found = []
    elm.set_timeout(80)
    try:
        for i, a in enumerate(addrs):
            if progress:
                progress(i + 1, len(addrs))
            if uds.tester_present(a):
                m = Module(a, bus, name_for(bus, a, make))
                found.append(m)
    finally:
        elm.set_timeout(400)
    for m in found:
        identify(uds, m)
    return found
