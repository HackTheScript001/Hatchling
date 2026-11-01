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


# ---- MOT readiness (mode 01 PID 01) ------------------------------------------------
CONTINUOUS = [(0, "Misfire"), (1, "Fuel system"), (2, "Components")]
PETROL_MONITORS = [(0, "Catalyst"), (1, "Heated catalyst"), (2, "Evaporative system"), (3, "Secondary air"),
                   (4, "A/C refrigerant"), (5, "Oxygen sensor"), (6, "Oxygen sensor heater"), (7, "EGR system")]
DIESEL_MONITORS = [(0, "NMHC catalyst"), (1, "NOx / AdBlue system"), (3, "Boost pressure"),
                   (5, "Exhaust gas sensor"), (6, "Particulate filter (DPF)"), (7, "EGR / VVT system")]


def readiness(data: bytes) -> dict:
    """Decodes PID 01: warning light, number of codes and each self-test as (name, supported, complete)."""
    if len(data) < 4:
        raise ElmError("Readiness reply too short")
    a, b, c, d = data[:4]
    diesel = bool(b & 0x08)
    mons = [(name, bool(b & (1 << bit)), not (b & (1 << (bit + 4)))) for bit, name in CONTINUOUS]
    for bit, name in (DIESEL_MONITORS if diesel else PETROL_MONITORS):
        mons.append((name, bool(c & (1 << bit)), not (d & (1 << bit))))
    return {"mil": bool(a & 0x80), "codes": a & 0x7F, "diesel": diesel, "monitors": mons}


def readiness_report(get_pid) -> dict:
    """get_pid(pid) -> data bytes (raises if unsupported). Adds the 'since codes were cleared' figures."""
    r = readiness(get_pid(0x01))
    for pid, key, fn in ((0x30, "warmups", lambda x: x[0]), (0x31, "distance_km", lambda x: x[0] * 256 + x[1]),
                         (0x21, "distance_mil_km", lambda x: x[0] * 256 + x[1])):
        try:
            r[key] = fn(get_pid(pid))
        except Exception:   # optional figures - older cars don't all have them
            pass
    sup = [m for m in r["monitors"] if m[1]]
    r["incomplete"] = [m[0] for m in sup if not m[2]]
    # MOT emissions testers generally accept one incomplete monitor on petrol cars from 2001 (none on others);
    # this is a guide only, the tester's rules decide.
    allowed = 0 if r["diesel"] else 1
    r["ready"] = not r["mil"] and len(r["incomplete"]) <= allowed
    return r


# ---- freeze frame (mode 02, frame 0) -------------------------------------------------
FREEZE_PIDS = [(0x04, "Engine load", "%", lambda d: round(d[0] * 100 / 255)),
               (0x05, "Coolant temp", "°C", lambda d: d[0] - 40),
               (0x06, "Short-term fuel trim", "%", lambda d: round((d[0] - 128) * 100 / 128, 1)),
               (0x07, "Long-term fuel trim", "%", lambda d: round((d[0] - 128) * 100 / 128, 1)),
               (0x0B, "Manifold pressure", "kPa", lambda d: d[0]),
               (0x0C, "Engine speed", "rpm", lambda d: (d[0] * 256 + d[1]) // 4),
               (0x0D, "Vehicle speed", "km/h", lambda d: d[0]),
               (0x0F, "Intake air temp", "°C", lambda d: d[0] - 40),
               (0x11, "Throttle position", "%", lambda d: round(d[0] * 100 / 255))]


def _freeze(self) -> dict:
    """The engine conditions saved when the first engine fault code was stored."""
    out = {}
    lines = self.elm.obd("020200", timeout=3.0)
    for p in _payload(lines, 0x42, self.is_can):
        if len(p) >= 4 and p[0] == 0x02:
            out["Code that saved it"] = decode_dtc(p[2], p[3]) if (p[2] or p[3]) else "none stored"
    for pid, name, unit, fn in FREEZE_PIDS:
        try:
            for p in _payload(self.elm.obd(f"02{pid:02X}00", timeout=2.0), 0x42, self.is_can):
                if len(p) > 2 and p[0] == pid:
                    out[name] = f"{fn(bytes(p[2:]))} {unit}"
                    break
        except ElmError:
            continue
    return out


GenericObd.freeze_frame = _freeze


# ---- misfire counts per cylinder (mode 06 on CAN cars) ----------------------------------
MISFIRE_MIDS = {0xA2 + i: i + 1 for i in range(12)}   # A2 = cylinder 1 ... AD = cylinder 12
EWMA_TID, LAST_CYCLE_TID = 0x0B, 0x0C


def parse_mode06(payload: bytes) -> list:
    """CAN mode 06 reply (after 0x46): 9-byte records MID TID UASID value min max -> [(mid, tid, value, min, max)]."""
    out = []
    for i in range(0, len(payload) - 8, 9):
        mid, tid, _unit = payload[i:i + 3]
        val, lo, hi = (int.from_bytes(payload[i + j:i + j + 2], "big") for j in (3, 5, 7))
        out.append((mid, tid, val, lo, hi))
    return out


def supported_mids(get06) -> set:
    """get06(mid) -> reply bytes after 0x46 (the first byte is the MID asked for)."""
    found = set()
    for base in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0):
        try:
            r = get06(base)
        except Exception:
            break
        if len(r) < 5 or r[0] != base:
            break
        bits = int.from_bytes(r[1:5], "big")
        for n in range(32):
            if bits & (1 << (31 - n)):
                found.add(base + n + 1)
        if not bits & 1:      # last bit says whether the next range exists
            break
    return found


def misfire_counts(get06) -> list:
    """[(cylinder, recent average, last drive)] from the engine computer's own misfire monitor.
    'Recent average' is the engine's smoothed count over the last ten drives."""
    mids = sorted(m for m in supported_mids(get06) if m in MISFIRE_MIDS)
    out = []
    for mid in mids:
        try:
            recs = parse_mode06(get06(mid))
        except Exception:
            continue
        vals = {tid: v for m, tid, v, _, _ in recs if m == mid}
        out.append((MISFIRE_MIDS[mid], vals.get(EWMA_TID), vals.get(LAST_CYCLE_TID)))
    return out


def _mode06(self, mid: int) -> bytes:
    """Generic mode 06 (CAN cars only): the reply after 0x46 from the first ECU that answers."""
    if not self.is_can:
        raise ElmError("Misfire counts need a CAN car (about 2008 on)")
    for p in _payload(self.elm.obd(f"06{mid:02X}", timeout=3.0), 0x46, True):
        if p and p[0] == mid:
            return bytes(p)
    raise ElmError(f"Test {mid:02X} not supported")


GenericObd.mode06 = _mode06
