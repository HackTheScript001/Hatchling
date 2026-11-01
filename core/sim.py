"""A fake switched ELM327 + car, so the whole app can be exercised without a vehicle.

It speaks the same text protocol as a real adapter (headers on, spaces on), including
multi-frame responses and manual multi-frame sends, so the real parsing code is tested.
"""
from __future__ import annotations

import math
import random
import time
from typing import Dict, List

from .elm import HS, MS


def _dtc(code: str, ftb: int = 0) -> bytes:
    letter = "PCBU".index(code[0])
    b0 = (letter << 6) | (int(code[1]) << 4) | int(code[2], 16)
    b1 = int(code[3:5], 16)
    return bytes([b0, b1, ftb])


class SimEcu:
    def __init__(self, addr, part, sw, asbuilt: Dict[int, bytes], dtcs=None, vin="WF0DXXGAKDCF00000"):
        self.addr = addr
        self.dids = {0xF190: vin.encode(), 0xF111: part.encode(), 0xF188: sw.encode(),
                     0xF113: (part[:-2] + "AB").encode()}
        self.asbuilt = {k: bytearray(v) for k, v in asbuilt.items()}
        self.dtcs = list(dtcs or [])
        self.session = 0x01
        self.live = {}       # did -> callable returning bytes
        self.routines = {}   # routine id -> seconds to "run"
        self._running = {}   # routine id -> finish time

    def handle(self, req: bytes) -> bytes:
        sid = req[0]
        neg = lambda code: bytes([0x7F, sid, code])
        if sid == 0x3E:
            return b"\x7E\x00"
        if sid == 0x10:
            self.session = req[1]
            return bytes([0x50, req[1], 0x00, 0x32, 0x01, 0xF4])
        if sid == 0x22:
            did = (req[1] << 8) | req[2]
            if did in self.dids:
                return bytes([0x62, req[1], req[2]]) + self.dids[did]
            if did in self.asbuilt:
                return bytes([0x62, req[1], req[2]]) + bytes(self.asbuilt[did])
            if did in self.live:
                return bytes([0x62, req[1], req[2]]) + self.live[did]()
            return neg(0x31)
        if sid == 0x2E:
            did = (req[1] << 8) | req[2]
            if self.session != 0x03:
                return neg(0x7F)
            if did not in self.asbuilt:
                return neg(0x31)
            if len(req) - 3 != len(self.asbuilt[did]):
                return neg(0x13)
            self.asbuilt[did] = bytearray(req[3:])
            return bytes([0x6E, req[1], req[2]])
        if sid == 0x19 and len(req) >= 6 and req[1] in (0x04, 0x06):
            dtc3 = bytes(req[2:5])
            st = next((s for r, s in self.dtcs if r == dtc3), None)
            if st is None:
                return neg(0x31)
            if req[1] == 0x04:  # one snapshot record: 2 DIDs (rpm, coolant)
                return bytes([0x59, 0x04, *dtc3, st, 0x01, 0x02, 0xDD, 0x01, 0x0C, 0x80, 0xDD, 0x05, 0x7A])
            return bytes([0x59, 0x06, *dtc3, st, 0x01, 0x03, 0x02, 0x12])  # occurrence counters
        if sid == 0x11:
            self.session = 0x01
            return bytes([0x51, req[1]])
        if sid == 0x31:
            rid = (req[2] << 8) | req[3]
            if rid not in self.routines:
                return neg(0x31)
            if req[1] == 0x01:
                if self.session != 0x03:
                    return neg(0x7F)
                self._running[rid] = time.monotonic() + self.routines[rid]
                return bytes([0x71, 0x01, req[2], req[3], 0x01])
            if req[1] == 0x03:
                end = self._running.get(rid)
                if end is None:
                    return neg(0x24)
                status = 0x00 if time.monotonic() >= end else 0x01
                return bytes([0x71, 0x03, req[2], req[3], status])
            if req[1] == 0x02:
                self._running.pop(rid, None)
                return bytes([0x71, 0x02, req[2], req[3]])
        if sid == 0x19:
            out = bytes([0x59, 0x02, 0xFF])
            for raw, st in self.dtcs:
                out += raw + bytes([st])
            return out
        if sid == 0x14:
            self.dtcs.clear()
            return b"\x54"
        if sid == 0x27:
            return bytes([0x67, req[1], 0, 0])  # zero seed = unlocked
        return neg(0x11)


class SimPcm(SimEcu):
    MISFIRES = {1: (0, 0), 2: (1, 0), 3: (14, 22), 4: (0, 1)}   # cylinder: (recent average, last drive)

    def handle(self, req: bytes) -> bytes:
        if req[0] == 0x06 and len(req) > 1:
            mid = req[1]
            bitmaps = {0x00: 0x00000001, 0x20: 0x00000001, 0x40: 0x00000001, 0x60: 0x00000001,
                       0x80: 0x00000001, 0xA0: 0x78000000}   # A2-A5 = cylinders 1-4
            if mid in bitmaps:
                return bytes([0x46, mid]) + bitmaps[mid].to_bytes(4, "big")
            if 0xA2 <= mid <= 0xA5:
                ewma, last = self.MISFIRES[mid - 0xA1]
                rec = lambda tid, v: bytes([mid, tid, 0x24]) + v.to_bytes(2, "big") + b"\0\0" + (0xFFFF).to_bytes(2, "big")
                return bytes([0x46]) + rec(0x0B, ewma) + rec(0x0C, last)
            return bytes([0x7F, 0x06, 0x12])
        if req[0] == 0x01:
            t = time.monotonic()
            rpm = 850 + 600 * (1 + math.sin(t / 2))
            vals = {
                0x0C: [int(rpm * 4) >> 8, int(rpm * 4) & 0xFF],
                0x0D: [int(25 + 20 * math.sin(t / 3))],
                0x05: [40 + 88 + random.randint(0, 1)],
                0x0F: [40 + 18],
                0x04: [int(60 + 40 * math.sin(t))],
                0x11: [int(40 + 30 * math.sin(t / 2))],
                0x0B: [int(35 + 20 * math.sin(t))],
                0x10: [0, int(300 + 200 * math.sin(t / 2)) & 0xFF],
                0x42: [0x36, 0xB0 + random.randint(0, 15)],
                0x2F: [150],
                0x01: [0x82, 0x07, 0xE5, 0x04],   # warning light on, 2 codes, evap test not finished
                0x30: [12],
                0x31: [0x00, 0xF0],
                0x21: [0x00, 0x2A],
            }
            if req[1] in vals:
                return bytes([0x41, req[1]] + vals[req[1]])
            return bytes([0x7F, 0x01, 0x12])
        return super().handle(req)


class SimKwpEcu(SimEcu):
    """An older module that only speaks KWP2000 diagnostics (2003-2010 style)."""

    def handle(self, req: bytes) -> bytes:
        if req[0] == 0x19:
            return bytes([0x7F, 0x19, 0x11])
        if req[0] == 0x18:
            out = bytes([0x58, len(self.dtcs)])
            for raw, st in self.dtcs:
                out += raw[:2] + bytes([st])
            return out
        if req[0] == 0x3E:
            return bytes([0x7F, 0x3E, 0x12])  # refuses UDS-style tester present, but it's there
        return super().handle(req)


def build_car() -> Dict[str, Dict[int, SimEcu]]:
    vin = "WF0DXXGAKDFA12345"
    bcm = SimEcu(0x726, "C1BT-14A073-AE", "C1BT-14C184-AH",
                 {0xDE00: bytes.fromhex("2A8040A6"), 0xDE01: bytes.fromhex("0C0A0000E1"),
                  0xDE02: bytes.fromhex("8100100000500000"), 0xDE03: bytes.fromhex("0300")},
                 [(_dtc("B1D02", 0x13), 0x08)], vin)
    bcm.live = {
        0x4028: lambda: bytes([82 + random.randint(0, 2)]),                           # battery charge %
        0x402A: lambda: bytes([0x30, 0xD4 + random.randint(0, 8)]),                   # battery voltage mV/1000
        0xD111: lambda: bytes([int(20 + 5 * math.sin(time.monotonic() / 4)) & 0xFF]), # interior temp
    }
    bcm.routines = {0x0F01: 2.0}   # demo: battery monitor reset
    car = {
        HS: {
            0x7E0: SimPcm(0x7E0, "D1BA-12A650-AFB", "E1B1-14C204-AB",
                          {0xDE00: bytes.fromhex("41C2"), 0xDE01: bytes.fromhex("000100")},
                          [(_dtc("P0420", 0x00), 0x08)], vin),
            0x760: SimEcu(0x760, "C1B1-2C405-AE", "C1B1-14C036-AC",
                          {0xDE00: bytes.fromhex("08A0")}, [], vin),
            0x737: SimEcu(0x737, "C1BT-14B321-AC", "C1BT-14C028-AB",
                          {0xDE00: bytes.fromhex("1F0300")}, [], vin),
            0x730: SimEcu(0x730, "C1B1-3D065-AB", "C1B1-14D003-AA", {}, [], vin),
            0x726: bcm,
        },
        MS: {
            0x726: bcm,
            0x720: SimEcu(0x720, "C1BT-10849-LK", "C1BT-14C026-AK",
                          {0xDE00: bytes.fromhex("BC02A044"), 0xDE01: bytes.fromhex("02200000"),
                           0xDE02: bytes.fromhex("0000")},
                          [(_dtc("U0155", 0x00), 0x2C)], vin),
            0x727: SimEcu(0x727, "C1BT-18C815-GA", "C1BT-14D099-BA",
                          {0xDE00: bytes.fromhex("01"), 0xDE01: bytes.fromhex("8400")}, [], vin),
            0x7D0: SimEcu(0x7D0, "E1BT-14D212-EB", "E1BT-14D544-AB",
                          {0xDE00: bytes.fromhex("0A"), 0xDE01: bytes.fromhex("0000")}, [], vin),
        },
    }
    car[HS][0x7E1] = SimKwpEcu(0x7E1, "6M2P-7J104-AB", "6M2P-14C204-AA", {}, [(_dtc("P0731", 0), 0x2F)], vin)
    car[HS][0x760].routines = {0x0E02: 3.0}  # demo: steering angle calibration
    car[HS][0x760].live = {0x2B06: lambda: bytes([0x00, int(40 + 30 * math.sin(time.monotonic()))])}
    return car


class SimTransport:
    """legacy=True pretends to be a 1990s/2000s Ford on J1850 PWM: generic OBD-II only."""

    def __init__(self, legacy: bool = False):
        self.legacy = legacy
        self.car = {} if legacy else build_car()
        self.pcm = SimPcm(0x7E0, "XS4F-12A650-AB", "-", {}, [(_dtc("P0171"), 0x08), (_dtc("P0420"), 0x08)],
                          "1FAFP34P63W123456") if legacy else (self.car[HS][0x7E0])
        self.bus = HS
        self.divisor = 4
        self.headers = True
        self.header = 0x7E0
        self.caf = True
        self.pending = None
        self.voltage = 12.6
        self.obd_mode = False

    def close(self):
        pass

    def _frames(self, rx: int, msg: bytes) -> List[str]:
        out = []

        def fmt(b):
            return f"{rx:03X} " + " ".join(f"{x:02X}" for x in b)
        if len(msg) <= 7:
            return [fmt(bytes([len(msg)]) + msg)]
        out.append(fmt(bytes([0x10 | (len(msg) >> 8), len(msg) & 0xFF]) + msg[:6]))
        rest, seq = msg[6:], 1
        while rest:
            out.append(fmt(bytes([0x20 | (seq & 0xF)]) + rest[:7]))
            rest, seq = rest[7:], seq + 1
        return out

    @staticmethod
    def _hex(b) -> str:
        return " ".join(f"{x:02X}" for x in b)

    def _obd(self, data: bytes) -> List[str]:
        """Generic OBD replies, headers off. J1850 style if legacy, CAN style otherwise."""
        pcm, mode = self.pcm, data[0]
        if mode == 0x01:
            r = pcm.handle(data) if data[1] != 0x00 else bytes([0x41, 0x00, 0xBE, 0x3E, 0xB8, 0x11])
            return [self._hex(r)] if r[0] == 0x41 else ["NO DATA"]
        if mode == 0x02 and len(data) >= 3:
            if data[1] == 0x02:
                raw = pcm.dtcs[0][0][:2] if pcm.dtcs else b"\0\0"
                return [self._hex(bytes([0x42, 0x02, 0x00]) + raw)]
            r = pcm.handle(bytes([0x01, data[1]]))
            return [self._hex(bytes([0x42, data[1], 0x00]) + r[2:])] if r[0] == 0x41 and pcm.dtcs else ["NO DATA"]
        if mode in (0x03, 0x07):
            codes = [raw[:2] for raw, st in pcm.dtcs if (st & 0x08) == (0x08 if mode == 0x03 else 0)] \
                if mode == 0x03 else []
            if self.legacy:  # J1850: 3 codes per line, padded, no count
                flat = b"".join(codes)
                lines = []
                for i in range(0, max(len(flat), 1), 6):
                    lines.append(self._hex(bytes([mode + 0x40]) + flat[i:i + 6].ljust(6, b"\0")))
                return lines
            return [self._hex(bytes([mode + 0x40, len(codes)]) + b"".join(codes))]
        if mode == 0x04:
            pcm.dtcs.clear()
            return ["44"]
        if mode == 0x09 and data[1] == 0x02:
            vin = pcm.dids[0xF190]
            if self.legacy:  # 5 lines: 49 02 seq + 4 bytes (first padded)
                v = b"\0\0\0" + vin
                return [self._hex(bytes([0x49, 0x02, i + 1]) + v[i * 4:i * 4 + 4]) for i in range(5)]
            msg = bytes([0x49, 0x02, 0x01]) + vin
            lines = ["014", "0: " + self._hex(msg[:6])]
            rest, n = msg[6:], 1
            while rest:
                lines.append(f"{n:X}: " + self._hex(rest[:7]))
                rest, n = rest[7:], n + 1
            return lines
        return ["NO DATA"]

    def send(self, cmd: str, timeout: float = 2.0) -> List[str]:
        time.sleep(0.003)
        c = cmd.replace(" ", "").upper()
        if c.startswith("AT"):
            a = c[2:]
            if a == "Z":
                return ["ELM327 v1.5 (Hatchling simulator)"]
            if a == "RV":
                return [f"{self.voltage:.1f}V"]
            if a == "DPN":
                return ["A1" if self.legacy else "A6"]
            if a == "SP6":
                self.bus, self.obd_mode = HS, False
            elif a.startswith("PBC1"):
                self.divisor = int(a[4:], 16)
            elif a == "SPB":
                self.bus, self.obd_mode = (MS if self.divisor == 4 else "HS2"), False
            elif a == "SP0":
                self.obd_mode = True
            elif a == "H0":
                self.headers = False
            elif a == "H1":
                self.headers = True
            elif a.startswith("SH"):
                self.header = int(a[2:], 16)
            elif a == "CAF0":
                self.caf = False
            elif a == "CAF1":
                self.caf = True
            return ["OK"]
        try:
            data = bytes.fromhex(c)
        except ValueError:
            return ["?"]       # unknown command, like a real ELM327 (e.g. STI on a non-OBDLink adapter)
        if self.obd_mode:
            return (["SEARCHING..."] if data[:2] == b"\x01\x00" else []) + self._obd(data)
        ecu = self.car.get(self.bus, {}).get(self.header)
        if ecu is None:
            return ["NO DATA"]
        rx = self.header + 8
        if not self.caf:  # manual ISO-TP frames from the tester
            pci = data[0] >> 4
            if pci == 1:
                n = ((data[0] & 0xF) << 8) | data[1]
                self.pending = (n, bytearray(data[2:8]))
                return [f"{rx:03X} 30 00 00 00 00 00 00 00"]
            if pci == 2 and self.pending:
                n, buf = self.pending
                buf.extend(data[1:8])
                if len(buf) >= n:
                    self.pending = None
                    return self._frames(rx, ecu.handle(bytes(buf[:n])))
                return ["NO DATA"]
            if pci == 0:
                return self._frames(rx, ecu.handle(data[1:1 + (data[0] & 0xF)]))
            return ["NO DATA"]
        return self._frames(rx, ecu.handle(data))


def sample_factory():
    """A factory As-Built file for the simulated Fiesta, as Ford would supply it (5 data bytes per line plus
    the checksum byte). It matches the simulator except auto headlights, which was 'off at the factory',
    so Changes since factory has something to show."""
    from .factory import FactoryData
    car = build_car()
    vin = "WF0DXXGAKDFA12345"
    seen = {}
    for bus in (HS, MS):
        for addr, ecu in car[bus].items():
            seen.setdefault(addr, ecu)
    lines = {}
    for addr, ecu in seen.items():
        for did, data in ecu.asbuilt.items():
            data = bytearray(data)
            if addr == 0x726 and did == 0xDE00:
                data[1] &= ~0x80 & 0xFF          # auto headlights off when built
            block = did - 0xDE00 + 1
            for i in range(0, len(data), 5):
                chunk = bytes(data[i:i + 5])
                line = i // 5 + 1
                cs = (sum(chunk) + (addr >> 8) + (addr & 0xFF) + block + line) & 0xFF
                lines[(addr, block, line)] = chunk + bytes([cs])
    return FactoryData(vin, lines, "Simulator sample factory file", "built in")
