"""ELM327 transport layer for switched HS/MS-CAN adapters.

Talks plain ELM327 AT commands over a serial (USB) port. Handles:
  * adapter init, battery voltage
  * switching between buses (HS-CAN, MS-CAN, HS-CAN on 3/11, generic OBD-II)
  * per-module header / receive filter / flow control setup
  * ISO-TP reassembly of single and multi-frame responses
"""
from __future__ import annotations

import re
import socket
import sys
import threading
import time
from typing import Callable, List, Optional

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError:  # allows the core + simulator to run without pyserial
    serial = None
    list_ports = None

from .buses import BUSES, HS, HS2, MS, OBD  # noqa: F401  (re-exported)

ERROR_STRINGS = ("CAN ERROR", "BUS ERROR", "BUFFER FULL", "UNABLE TO CONNECT",
                 "FB ERROR", "DATA ERROR", "LV RESET", "STOPPED", "ACT ALERT", "?")


class ElmError(Exception):
    pass


# USB-serial chips used by ELM327 adapters (vendor id, product id) -> name, driver page
USB_CHIPS = {
    (0x0403, 0x6001): ("FTDI FT232R", "https://ftdichip.com/drivers/vcp-drivers/"),
    (0x0403, 0x6015): ("FTDI FT231X", "https://ftdichip.com/drivers/vcp-drivers/"),
    (0x1A86, 0x7523): ("WCH CH340", "http://www.wch-ic.com/downloads/CH341SER_ZIP.html"),
    (0x1A86, 0x5523): ("WCH CH341", "http://www.wch-ic.com/downloads/CH341SER_ZIP.html"),
    (0x10C4, 0xEA60): ("Silicon Labs CP210x", "https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers"),
    (0x067B, 0x2303): ("Prolific PL2303", "https://www.prolific.com.tw/"),
}
DRIVER_PAGES = {"FTDI": "https://ftdichip.com/drivers/vcp-drivers/",
                "CH340": "http://www.wch-ic.com/downloads/CH341SER_ZIP.html"}


def available_ports() -> List[str]:
    if list_ports is None:
        return []
    return [p.device for p in list_ports.comports()]


def describe_ports() -> List[tuple]:
    """(device, label) for each serial port, naming the adapter's USB chip when known."""
    if list_ports is None:
        return []
    out = []
    for p in list_ports.comports():
        chip = USB_CHIPS.get((p.vid, p.pid)) if p.vid is not None else None
        desc = p.description if p.description and p.description != "n/a" else ""
        dev = p.device or ""
        if dev.startswith("/dev/ttyS") and p.vid is None:
            continue           # Linux lists dozens of built-in serial ports that are never an adapter
        if dev.startswith("/dev/rfcomm"):
            out.append((dev, f"{dev} - Bluetooth adapter"))
            continue
        if "bluetooth" in desc.lower():
            # Windows makes two COM ports per paired Bluetooth adapter - the 'outgoing' one is the one to use
            name = "Bluetooth adapter (if it doesn't answer, try the other Bluetooth COM port)"
        else:
            name = chip[0] if chip else desc
        out.append((p.device, f"{p.device} - {name}" if name else p.device))
    return out


WIFI_DEFAULT = "192.168.0.10:35000"   # what most Wi-Fi ELM327 adapters use


def parse_wifi(addr: str) -> tuple:
    addr = (addr or "").strip().removeprefix("tcp://")
    m = re.fullmatch(r"([A-Za-z0-9.-]{1,253}):(\d{1,5})", addr)
    if not m or not 0 < int(m.group(2)) < 65536:
        raise ElmError("Enter the Wi-Fi adapter's address like 192.168.0.10:35000")
    return m.group(1), int(m.group(2))


def open_transport(port: str, baud: int = 38400):
    """A serial port (USB or Bluetooth) or tcp://host:port for a Wi-Fi adapter."""
    if port.startswith("tcp://"):
        return TcpTransport(*parse_wifi(port))
    return SerialTransport(port, baud)


class SerialTransport:
    """Raw line I/O with the adapter. send() returns the response lines before '>'."""

    def __init__(self, port: str, baud: int = 38400):
        if serial is None:
            raise ElmError("pyserial is not installed (pip install pyserial)")
        try:
            self.ser = serial.Serial(port, baud, timeout=0.05)
        except serial.SerialException as e:
            if "ermission" in str(e) and not sys.platform.startswith("win"):
                raise ElmError(f"No permission to use {port}. Run this once in a terminal, then log out and back in: "
                               "sudo usermod -aG dialout $USER")
            raise ElmError(f"Couldn't open {port}: {e}")

    def send(self, cmd: str, timeout: float = 2.0) -> List[str]:
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode("ascii"))
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.ser.read(512)
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        else:
            raise ElmError(f"Adapter did not answer '{cmd}' in {timeout:.1f}s")
        text = buf.decode("ascii", "ignore").replace(">", "")
        lines = [l.strip() for l in re.split(r"[\r\n]+", text) if l.strip()]
        if lines and lines[0].replace(" ", "").upper() == cmd.replace(" ", "").upper():
            lines = lines[1:]  # echo
        return lines

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class TcpTransport:
    """Wi-Fi ELM327 adapters: the same text protocol over a TCP connection. Join the adapter's Wi-Fi
    network on the PC first."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise ElmError(f"Couldn't reach the Wi-Fi adapter at {host}:{port} - join its Wi-Fi network first ({e})")
        self.sock.settimeout(0.05)

    def _drain(self):
        try:
            while self.sock.recv(4096):
                pass
        except OSError:
            pass

    def send(self, cmd: str, timeout: float = 2.0) -> List[str]:
        self._drain()
        try:
            self.sock.sendall((cmd + "\r").encode("ascii"))
        except OSError as e:
            raise ElmError(f"Wi-Fi adapter connection lost ({e})")
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                raise ElmError(f"Wi-Fi adapter connection lost ({e})")
            if not chunk:
                raise ElmError("The Wi-Fi adapter closed the connection")
            buf += chunk
            if b">" in buf:
                break
        else:
            raise ElmError(f"Adapter did not answer '{cmd}' in {timeout:.1f}s")
        text = buf.decode("ascii", "ignore").replace(">", "")
        lines = [l.strip() for l in re.split(r"[\r\n]+", text) if l.strip()]
        if lines and lines[0].replace(" ", "").upper() == cmd.replace(" ", "").upper():
            lines = lines[1:]
        return lines

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def parse_isotp(lines: List[str], rx_id: int) -> Optional[bytes]:
    """Reassemble ISO-TP frames printed by the ELM (ATH1, ATS1, ATCAF1).

    Returns the last complete message that is not 'response pending' (7F xx 78),
    or None for NO DATA.
    """
    rx_hex = f"{rx_id:03X}"
    messages: List[bytes] = []
    current: Optional[bytearray] = None
    expected = 0
    for line in lines:
        up = line.upper()
        if up.startswith("SEARCHING"):
            continue
        if "NO DATA" in up:
            continue
        if any(up.startswith(e) or up == e for e in ERROR_STRINGS):
            raise ElmError(f"Adapter error: {line}")
        tokens = line.split()
        if not tokens or tokens[0].upper() != rx_hex:
            continue
        try:
            data = [int(t, 16) for t in tokens[1:]]
        except ValueError:
            continue
        if not data:
            continue
        pci = data[0] >> 4
        if pci == 0:
            n = data[0] & 0x0F
            messages.append(bytes(data[1:1 + n]))
        elif pci == 1:
            expected = ((data[0] & 0x0F) << 8) | data[1]
            current = bytearray(data[2:8])
        elif pci == 2 and current is not None:
            current.extend(data[1:8])
            if len(current) >= expected:
                messages.append(bytes(current[:expected]))
                current = None
    final = [m for m in messages if not (len(m) >= 3 and m[0] == 0x7F and m[2] == 0x78)]
    if final:
        return final[-1]
    if messages:  # only "response pending" seen
        raise ElmError("Module kept answering 'response pending' - try again")
    return None


class ELM327:
    def __init__(self, transport, log: Optional[Callable[[str], None]] = None):
        self.t = transport
        self.lock = threading.RLock()
        self.bus: Optional[str] = None
        self.version = ""
        self._target = None
        self._timeout_ms = None
        self.log = log or (lambda s: None)
        self.trace = False
        self.chip = "ELM"

    # ---- low level -------------------------------------------------
    def cmd(self, c: str, timeout: float = 2.0) -> List[str]:
        with self.lock:
            if self.trace:
                self.log(f">> {c}")
            r = self.t.send(c, timeout)
            if self.trace:
                self.log("<< " + " | ".join(r))
            return r

    def _ok(self, c: str):
        r = self.cmd(c)
        if not any("OK" in l.upper() for l in r):
            raise ElmError(f"{c} rejected by adapter: {r}")

    # ---- setup -----------------------------------------------------
    def initialise(self):
        with self.lock:
            r = self.cmd("ATZ", timeout=5)
            self.version = next((l for l in r if "ELM" in l.upper()), "unknown")
            for c in ("ATE0", "ATL0", "ATS1", "ATH1", "ATCAF1", "ATAT1"):
                self._ok(c)
            # OBDLink and other STN-chip adapters answer STI with their real name (they all say ELM327 to ATZ)
            try:
                sti = self.cmd("STI", timeout=1.0)
            except ElmError:
                sti = []
            name = next((l for l in sti if l.upper().startswith(("STN", "OBDLINK"))), "")
            self.chip = "STN" if name else "ELM"
            if name:
                self.version = f"{name} (ELM327 compatible)"
            self.set_bus(HS, force=True)
            self.log(f"Adapter ready: {self.version}")

    def set_bus(self, bus: str, force: bool = False):
        with self.lock:
            if bus == self.bus and not force:
                return
            b = BUSES[bus]
            leaving_obd = self.bus is not None and BUSES[self.bus].protocol == "obd"
            if b.protocol == "obd":
                self._ok("ATH0")            # plain replies, adapter picks J1850/ISO/KWP/CAN itself
                self._ok("ATCRA")           # clear receive filter
                self._ok("ATSP0")
            else:
                if leaving_obd:
                    self._ok("ATH1")
                if b.kbps == 500 and b.switch == "HS":
                    self._ok("ATSP6")       # ISO 15765-4 CAN 11-bit 500k on pins 6/14
                else:
                    self._ok(f"ATPBC1{500 // b.kbps:02X}")  # user CAN B: 11-bit, ISO-TP, 500k / divisor
                    self._ok("ATSPB")
            self.bus = bus
            self._target = None
            self.log(f"Bus set to {b.label}")

    def obd(self, payload: str, timeout: float = 6.0) -> List[str]:
        """Raw generic OBD request on the OBD bus (headers off). Returns reply lines."""
        with self.lock:
            lines = self.cmd(payload, timeout)
            return [l for l in lines if not l.upper().startswith("SEARCHING")]

    def protocol_number(self) -> str:
        r = self.cmd("ATDPN")
        return r[0].upper().lstrip("A") if r else ""

    def set_timeout(self, ms: int):
        with self.lock:
            if self._timeout_ms == ms:
                return
            self._ok(f"ATST{max(1, min(255, ms // 4)):02X}")
            self._timeout_ms = ms

    def voltage(self) -> Optional[float]:
        r = self.cmd("ATRV")
        for l in r:
            m = re.search(r"(\d+(?:\.\d+)?)\s*V", l.upper())
            if m:
                return float(m.group(1))
        return None

    def _set_target(self, tx: int):
        if self._target == tx:
            return
        rx = tx + 8
        self._ok(f"ATSH{tx:03X}")
        self._ok(f"ATCRA{rx:03X}")
        self._ok(f"ATFCSH{tx:03X}")
        self._ok("ATFCSD300000")
        self._ok("ATFCSM1")
        self._target = tx

    # ---- requests --------------------------------------------------
    def request(self, tx: int, payload: bytes, timeout: float = 3.0) -> Optional[bytes]:
        with self.lock:
            self._set_target(tx)
            if len(payload) <= 7:
                lines = self.cmd(payload.hex(" ").upper(), timeout)
                return parse_isotp(lines, tx + 8)
            return self._request_multiframe(tx, payload, timeout)

    def _request_multiframe(self, tx: int, payload: bytes, timeout: float) -> Optional[bytes]:
        """Manual ISO-TP send for requests > 7 bytes (e.g. long As-Built writes).

        Turns off auto formatting, sends the First Frame, checks for a Flow Control
        frame from the module, then sends Consecutive Frames. The module's answer to
        the last frame is parsed as the response.
        """
        rx = tx + 8
        n = len(payload)
        frames = [bytes([0x10 | (n >> 8), n & 0xFF]) + payload[:6]]
        rest, seq = payload[6:], 1
        while rest:
            frames.append(bytes([0x20 | (seq & 0x0F)]) + rest[:7])
            rest, seq = rest[7:], seq + 1
        frames = [f.ljust(8, b"\x00") for f in frames]
        self._ok("ATCAF0")
        try:
            lines = self.cmd(frames[0].hex(" ").upper(), timeout)
            fc_ok = False
            for l in lines:
                t = l.split()
                if len(t) >= 2 and t[0].upper() == f"{rx:03X}" and t[1].upper().startswith("3"):
                    if t[1].upper() != "30":
                        raise ElmError(f"Module refused multi-frame (flow control {t[1]})")
                    fc_ok = True
            if not fc_ok:
                raise ElmError("No flow-control frame from module")
            for f in frames[1:-1]:
                self.cmd(f.hex(" ").upper(), 0.5)
            lines = self.cmd(frames[-1].hex(" ").upper(), timeout)
            return parse_isotp(lines, rx)
        finally:
            self._ok("ATCAF1")

    def close(self):
        self.t.close()


BAUD_CANDIDATES = (38400, 115200, 57600, 500000, 9600, 230400)


def find_baud(port: str, preferred: Optional[int] = None, make_transport=None,
              log: Callable[[str], None] = lambda s: None) -> int:
    """Tries each common speed with a quick ATI and returns the first that answers like an ELM327.
    make_transport(port, baud) lets tests supply a fake adapter."""
    make = make_transport or (lambda p, b: SerialTransport(p, b))
    order = ([preferred] if preferred else []) + [b for b in BAUD_CANDIDATES if b != preferred]
    for baud in order:
        try:
            t = make(port, baud)
        except Exception as e:  # port busy / missing
            raise ElmError(f"Couldn't open {port}: {e}")
        try:
            r = t.send("ATI", timeout=1.0)
            if any("ELM" in line.upper() for line in r):
                log(f"Adapter answered at {baud} baud")
                return baud
        except ElmError:
            pass
        finally:
            t.close()
    raise ElmError(f"No adapter answered on {port} at any speed. Check it's plugged in and the ignition is on.")
