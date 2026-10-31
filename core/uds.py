"""UDS (ISO 14229) services used for Ford module diagnostics."""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from .elm import ELM327

NRC = {
    0x10: "General reject", 0x11: "Service not supported", 0x12: "Sub-function not supported",
    0x13: "Incorrect message length", 0x14: "Response too long", 0x22: "Conditions not correct",
    0x24: "Request sequence error", 0x31: "Request out of range", 0x33: "Security access denied",
    0x35: "Invalid key", 0x36: "Exceeded number of attempts", 0x37: "Required time delay not expired",
    0x72: "General programming failure", 0x78: "Response pending",
    0x7E: "Sub-function not supported in active session",
    0x7F: "Service not supported in active session",
}

DEFAULT_SESSION = 0x01
EXTENDED_SESSION = 0x03


class UdsError(Exception):
    pass


class NoResponse(UdsError):
    pass


class NegativeResponse(UdsError):
    def __init__(self, sid: int, code: int):
        self.sid, self.code = sid, code
        super().__init__(f"Service 0x{sid:02X} refused: {NRC.get(code, 'Unknown')} (0x{code:02X})")


# seed -> key function, supplied by you per module if a module needs security access.
SecurityHook = Callable[[int, int, bytes], bytes]  # (tx_addr, level, seed) -> key


class UDS:
    def __init__(self, elm: ELM327):
        self.elm = elm
        self.security_hook: Optional[SecurityHook] = None

    def _req(self, tx: int, payload, timeout: float = 3.0) -> bytes:
        payload = bytes(payload)
        resp = self.elm.request(tx, payload, timeout)
        if resp is None:
            raise NoResponse(f"No response from module 0x{tx:03X}")
        if resp[0] == 0x7F and len(resp) >= 3:
            raise NegativeResponse(resp[1], resp[2])
        if resp[0] != payload[0] + 0x40:
            raise UdsError(f"Unexpected reply {resp.hex(' ')}")
        return resp

    # ---- basic services -------------------------------------------
    def tester_present(self, tx: int) -> bool:
        """True if anything answers - a refusal from an older KWP2000 module still means it's there."""
        try:
            self._req(tx, [0x3E, 0x00], timeout=0.6)
            return True
        except NegativeResponse:
            return True
        except UdsError:
            return False

    def session(self, tx: int, session: int):
        self._req(tx, [0x10, session])

    def read_did(self, tx: int, did: int) -> bytes:
        r = self._req(tx, [0x22, did >> 8, did & 0xFF])
        return r[3:]

    def write_did(self, tx: int, did: int, data: bytes):
        self._req(tx, [0x2E, did >> 8, did & 0xFF, *data], timeout=5.0)

    def read_dtcs(self, tx: int, status_mask: int = 0xFF) -> List[Tuple[bytes, int]]:
        """UDS 19 02 (2011+ Fords: 3-byte DTCs + status). Falls back to KWP2000 18 02
        (roughly 2003-2010 CAN Fords: 2-byte DTCs + status) when a module doesn't speak UDS."""
        try:
            r = self._req(tx, [0x19, 0x02, status_mask], timeout=4.0)
        except NegativeResponse as e:
            if e.code not in (0x11, 0x12, 0x7F):
                raise
            return self.read_dtcs_kwp(tx)
        body = r[3:]
        out = []
        for i in range(0, len(body) - 3, 4):
            out.append((body[i:i + 3], body[i + 3]))
        return out

    def read_dtcs_kwp(self, tx: int) -> List[Tuple[bytes, int]]:
        """KWP2000 ReadDTCByStatus (18 02 FF 00): count, then 2-byte DTC + status each."""
        r = self._req(tx, [0x18, 0x02, 0xFF, 0x00], timeout=4.0)
        body = r[2:]
        return [(body[i:i + 2], body[i + 2]) for i in range(0, len(body) - 2, 3)]

    def clear_dtcs(self, tx: int):
        self._req(tx, [0x14, 0xFF, 0xFF, 0xFF], timeout=5.0)

    def ecu_reset(self, tx: int, reset_type: int = 0x01):
        """Service 11: 01 hard reset, 03 soft reset."""
        self._req(tx, [0x11, reset_type], timeout=5.0)

    def routine(self, tx: int, routine_id: int, sub: int = 0x01, data: bytes = b"", timeout: float = 10.0) -> bytes:
        """Service 31: sub 01 start, 02 stop, 03 request results. Returns status bytes after the ID."""
        r = self._req(tx, [0x31, sub, routine_id >> 8, routine_id & 0xFF, *data], timeout=timeout)
        return r[4:]

    def dtc_snapshot(self, tx: int, dtc3: bytes, record: int = 0xFF) -> bytes:
        """Service 19 04: freeze-frame data stored with a code (raw, after the DTC and status)."""
        r = self._req(tx, [0x19, 0x04, *dtc3, record], timeout=4.0)
        return r[6:]

    def dtc_extended(self, tx: int, dtc3: bytes, record: int = 0xFF) -> bytes:
        """Service 19 06: extended data (counters, occurrence info)."""
        r = self._req(tx, [0x19, 0x06, *dtc3, record], timeout=4.0)
        return r[6:]

    def security_access(self, tx: int, level: int = 0x01):
        r = self._req(tx, [0x27, level])
        seed = r[2:]
        if not any(seed):
            return  # already unlocked
        if self.security_hook is None:
            raise UdsError("Module needs security access and no key algorithm is configured")
        key = self.security_hook(tx, level, seed)
        self._req(tx, [0x27, level + 1, *key])

    def obd_pid(self, pid: int, tx: int = 0x7E0) -> bytes:
        """OBD-II mode 01 request to the PCM."""
        r = self._req(tx, [0x01, pid], timeout=1.0)
        return r[2:]
