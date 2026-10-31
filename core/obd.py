"""Generic OBD-II (SAE J1979) over any protocol the ELM327 supports: J1850 PWM/VPW,
ISO 9141-2, KWP2000, CAN. This is what works on 1996-2007 Fords without CAN module
access: engine fault codes, clearing them, VIN and live data."""
from __future__ import annotations

import re
from typing import List, Optional

from .elm import ELM327, ElmError

CAN_PROTOCOLS = {"6", "7", "8", "9"}
PROTOCOL_NAMES = {
    "1": "SAE J1850 PWM (Ford SCP)", "2": "SAE J1850 VPW", "3": "ISO 9141-2", "4": "ISO 14230 KWP (5 baud)",
    "5": "ISO 14230 KWP (fast)", "6": "ISO 15765 CAN 11/500", "7": "ISO 15765 CAN 29/500",
    "8": "ISO 15765 CAN 11/250", "9": "ISO 15765 CAN 29/250",
}


def _bytes(line: str) -> List[int]:
    line = re.sub(r"^\s*[0-9A-F]:\s*", "", line.upper())  # strip CAN multi-frame index "0: "
    out = []
    for t in line.split():
        try:
            out.append(int(t, 16))
        except ValueError:
            return []
    return out


def _check(lines: List[str]):
    for l in lines:
        u = l.upper()
        if "UNABLE TO CONNECT" in u or "BUS INIT" in u and "ERROR" in u:
            raise ElmError("No OBD-II reply - check the ignition is on")


def _payload(lines: List[str], service: int, is_can: bool) -> List[List[int]]:
    """Returns the payload of each ECU response (after the service byte)."""
    _check(lines)
    if is_can:
        # single frame: "43 02 04 20 01 33"; multi-frame: "00A" then "0: 43 04 ..." "1: ..."
        frames = [l for l in lines if re.match(r"^\s*[0-9A-F]:", l.upper())]
        if frames:
            data = [b for f in frames for b in _bytes(f)]
            return [data[1:]] if data and data[0] == service else []
        out = []
        for l in lines:
            b = _bytes(l)
            if b and b[0] == service:
                out.append(b[1:])
        return out
    out = []
    for l in lines:
        b = _bytes(l)
        if b and b[0] == service:
            out.append(b[1:])
    return out


def decode_dtc(b0: int, b1: int) -> str:
    return f"{'PCBU'[b0 >> 6]}{(b0 >> 4) & 3}{b0 & 0xF:X}{b1 >> 4:X}{b1 & 0xF:X}"


class GenericObd:
    def __init__(self, elm: ELM327):
        self.elm = elm
        self.protocol = ""

    @property
    def is_can(self) -> bool:
        return self.protocol in CAN_PROTOCOLS

    def connect(self) -> str:
        """Wakes the car's OBD bus (adapter auto-detects protocol). Returns protocol name."""
        lines = self.elm.obd("0100", timeout=12.0)
        _check(lines)
        if not any(_bytes(l)[:1] == [0x41] for l in lines):
            raise ElmError("No OBD-II reply - check the ignition is on")
        self.protocol = self.elm.protocol_number()
        return PROTOCOL_NAMES.get(self.protocol, f"protocol {self.protocol}")

    def _dtcs(self, service: int) -> List[str]:
        out = []
        for p in _payload(self.elm.obd(f"{service - 0x40:02X}"), service, self.is_can):
            if self.is_can and p:
                p = p[1:]  # CAN replies start with a DTC count
            for i in range(0, len(p) - 1, 2):
                if p[i] or p[i + 1]:
                    out.append(decode_dtc(p[i], p[i + 1]))
        return sorted(set(out))

    def stored_dtcs(self) -> List[str]:
        return self._dtcs(0x43)

    def pending_dtcs(self) -> List[str]:
        return self._dtcs(0x47)

    def clear_dtcs(self):
        lines = self.elm.obd("04")
        if not any(_bytes(l)[:1] == [0x44] for l in lines):
            raise ElmError("The engine computer didn't confirm the clear - engine must be off, ignition on")

    def vin(self) -> Optional[str]:
        payloads = _payload(self.elm.obd("0902"), 0x49, self.is_can)
        data = []
        for p in payloads:
            if not p or p[0] != 0x02:
                continue
            body = p[1:]
            if self.is_can and body:
                body = body[1:]  # number-of-items byte
            elif body:
                body = body[1:]  # message sequence number on J1850/ISO
            data.extend(body)
        vin = bytes(b for b in data if 32 < b < 127).decode("ascii", "ignore")
        return vin[-17:] if len(vin) >= 17 else None

    def pid(self, pid: int) -> bytes:
        for p in _payload(self.elm.obd(f"01{pid:02X}", timeout=3.0), 0x41, self.is_can):
            if p and p[0] == pid:
                return bytes(p[1:])
        raise ElmError(f"PID {pid:02X} not supported")
