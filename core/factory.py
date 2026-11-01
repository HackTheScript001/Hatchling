"""Ford factory As-Built data: import the file for a VIN, store it, compare it with the car.

Ford publishes each vehicle's factory module configuration (As-Built) by VIN. Download it
before changing anything, import it here, and Hatchling can show every bit that's changed
since the car left the factory and turn those changes into features.

The importer is deliberately tolerant: it accepts Ford's XML download, a saved web page
or plain copied text - anything containing lines labelled like 7E0-01-01 followed by hex.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .asbuilt import Blocks
from .paths import user_dir

MAX_FILE_BYTES = 5 * 1024 * 1024
LABEL = re.compile(r"^\s*([0-9A-Fa-f]{3})-(\d{2})-(\d{2})\s*$")
LABEL_IN_TEXT = re.compile(r"\b([0-9A-Fa-f]{3})-(\d{2})-(\d{2})\b")
HEX_TOKEN = re.compile(r"^[0-9A-Fa-f]{2,64}$")   # grouped (A1B2 C3) or one run (A1B2C3)
VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
FACTORY_DIR = user_dir("factory")


class FactoryError(Exception):
    pass


@dataclass
class FactoryData:
    vin: str
    lines: Dict[Tuple[int, int, int], bytes] = field(default_factory=dict)   # (addr, block, line) -> raw bytes
    source: str = ""
    imported: str = ""

    @property
    def modules(self) -> List[int]:
        return sorted({a for a, _, _ in self.lines})

    def checksum_rate(self) -> float:
        """Share of lines whose last byte is the usual Ford checksum (sum of address, block, line, data)."""
        if not self.lines:
            return 0.0
        hits = 0
        for (addr, block, line), data in self.lines.items():
            if len(data) > 1 and (sum(data[:-1]) + (addr >> 8) + (addr & 0xFF) + block + line) & 0xFF == data[-1]:
                hits += 1
        return hits / len(self.lines)

    def blocks(self, addr: int, drop_checksum: Optional[bool] = None) -> Blocks:
        """Module config as DID blocks (block 01 -> DE00 ...), lines joined in order."""
        if drop_checksum is None:
            drop_checksum = self.checksum_rate() >= 0.6
        parts: Dict[int, Dict[int, bytes]] = {}
        for (a, block, line), data in self.lines.items():
            if a != addr or block < 1:
                continue
            body = data[:-1] if drop_checksum and len(data) > 1 else data
            parts.setdefault(0xDE00 + block - 1, {})[line] = body
        return {did: b"".join(p[k] for k in sorted(p)) for did, p in parts.items()}


# ---- parsing -------------------------------------------------------------------
def _hex_bytes(tokens: List[str]) -> bytes:
    h = "".join(t for t in tokens if HEX_TOKEN.match(t))
    if len(h) % 2:
        h = h[:-1]
    return bytes.fromhex(h) if h else b""


def _parse_xml(text: str) -> Tuple[str, Dict[Tuple[int, int, int], bytes]]:
    root = ET.fromstring(text)
    lines: Dict[Tuple[int, int, int], bytes] = {}
    vin = ""
    for el in root.iter():
        tag = el.tag.split("}")[-1].upper()
        if not vin and tag == "VIN" and el.text and VIN_RE.search(el.text.upper()):
            vin = VIN_RE.search(el.text.upper()).group(1)
        for k, v in el.attrib.items():
            if not vin and k.upper() == "VIN" and VIN_RE.search(v.upper()):
                vin = VIN_RE.search(v.upper()).group(1)
            m = LABEL.match(v)
            if m:
                # hex codes are the element's text and/or its children's text
                tokens = []
                for sub in el.iter():
                    if sub.text:
                        tokens += sub.text.split()
                data = _hex_bytes(tokens)
                if data:
                    lines[(int(m.group(1), 16), int(m.group(2)), int(m.group(3)))] = data
    return vin, lines


def _parse_text(text: str) -> Tuple[str, Dict[Tuple[int, int, int], bytes]]:
    """Copied text or a saved web page: label, then hex groups up to the next label."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines: Dict[Tuple[int, int, int], bytes] = {}
    hits = list(LABEL_IN_TEXT.finditer(text))
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        chunk = text[m.end():end]
        tokens = []
        for t in re.split(r"[\s,;|]+", chunk):
            if not t:
                continue
            if HEX_TOKEN.match(t):
                tokens.append(t)
            elif tokens:
                break   # stop at the first non-hex word after the codes
        data = _hex_bytes(tokens)
        if data:
            lines[(int(m.group(1), 16), int(m.group(2)), int(m.group(3)))] = data
    vin_m = VIN_RE.search(text.upper())
    return (vin_m.group(1) if vin_m else ""), lines


def parse(text: str, source: str = "") -> FactoryData:
    vin, lines = "", {}
    stripped = text.lstrip()
    if stripped.startswith("<") and not stripped[:200].lower().startswith(("<!doctype html", "<html")):
        try:
            vin, lines = _parse_xml(stripped)
        except ET.ParseError:
            lines = {}
    if not lines:
        vin2, lines = _parse_text(text)
        vin = vin or vin2
    if not lines:
        raise FactoryError("No As-Built lines found. The file should contain lines labelled like 7E0-01-01 "
                           "followed by hex codes.")
    return FactoryData(vin, lines, source, time.strftime("%Y-%m-%d %H:%M"))


def load_file(path: str) -> FactoryData:
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise FactoryError("That file is too big to be an As-Built file.")
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            if "-01-01" in text or "<" in text:
                break
        except UnicodeDecodeError:
            continue
    return parse(text, os.path.basename(path))


# ---- storage per VIN -------------------------------------------------------------
def _path(vin: str) -> str:
    return os.path.join(FACTORY_DIR, f"{vin or 'unknown'}.json")


def save(data: FactoryData) -> str:
    os.makedirs(FACTORY_DIR, exist_ok=True)
    path = _path(data.vin)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"vin": data.vin, "source": data.source, "imported": data.imported,
                   "lines": {f"{a:03X}-{b:02d}-{l:02d}": v.hex().upper() for (a, b, l), v in sorted(data.lines.items())}},
                  f, indent=1)
    return path


def load(vin: str) -> Optional[FactoryData]:
    try:
        with open(_path(vin), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    lines = {}
    for k, v in d.get("lines", {}).items():
        m = LABEL.match(k)
        if m:
            lines[(int(m.group(1), 16), int(m.group(2)), int(m.group(3)))] = bytes.fromhex(v)
    return FactoryData(d.get("vin", vin), lines, d.get("source", ""), d.get("imported", ""))


def stored() -> List[Tuple[str, str]]:
    """(VIN, imported) for every factory file saved on this computer."""
    out = []
    if not os.path.isdir(FACTORY_DIR):
        return out
    for fn in sorted(os.listdir(FACTORY_DIR)):
        if fn.endswith(".json") and VIN_RE.fullmatch(fn[:-5]):
            d = load(fn[:-5])
            if d:
                out.append((d.vin, d.imported))
    return out


# ---- comparing with the car -----------------------------------------------------
def align(factory: Blocks, car: Blocks) -> Blocks:
    """Fits factory blocks to the lengths the module actually reports (drops a trailing
    checksum byte if that's the only difference). Blocks that can't be matched are left out."""
    out = {}
    for did, fb in factory.items():
        cb = car.get(did)
        if cb is None:
            continue
        if len(fb) == len(cb):
            out[did] = fb
        elif len(fb) == len(cb) + 1:
            out[did] = fb[:-1]
    return out


def compare_cars(own: FactoryData, donor: FactoryData) -> Dict[int, list]:
    """Per module address in both files: bytes that differ between your car's factory build
    (before = off) and the donor's (after = on)."""
    from .finder import compare
    out = {}
    for addr in sorted(set(own.modules) & set(donor.modules)):
        a, b = own.blocks(addr), donor.blocks(addr)
        ch = compare(a, b)
        if ch:
            out[addr] = ch
    return out


# ---- sharing a factory file (anonymised) -------------------------------------------
# Options owners tick so cars with and without each one can be compared.
OPTIONS = [
    "Cruise control", "Speed limiter", "Auto headlights", "Rain-sensing wipers", "Front fog lights",
    "LED daytime running lights", "Rear parking sensors", "Front parking sensors", "Reversing camera",
    "Keyless entry", "Push-button start", "Heated windscreen", "Heated front seats", "Heated steering wheel",
    "Automatic climate control", "Electric folding mirrors", "Tyre pressure monitoring", "SYNC infotainment",
    "SYNC 3", "Satnav", "DAB radio", "Lane keeping aid", "Traffic sign recognition", "Automatic gearbox",
]
_ASCII_VIN = re.compile(rb"[A-HJ-NPR-Z0-9]{17}")


def anonymise(data: FactoryData, car: Dict[str, str], options: List[str]) -> dict:
    """The factory file without anything that identifies the car: no VIN, and any line whose bytes
    spell out a VIN (some modules keep one) is left out. car = {make, model, year, engine, trim, market}."""
    lines = {}
    dropped = 0
    for (addr, block, line), b in sorted(data.lines.items()):
        if _ASCII_VIN.search(b) or (data.vin and data.vin.encode() in b):
            dropped += 1
            continue
        lines[f"{addr:03X}-{block:02d}-{line:02d}"] = b.hex().upper()
    clean = lambda v, n: re.sub(r"[^A-Za-z0-9 .\-+/()]", "", str(v or ""))[:n].strip()
    year = clean(car.get("year"), 4)
    return {
        "schema": 1,
        "make": clean(car.get("make"), 20) or "Ford",
        "model": clean(car.get("model"), 40),
        "year": int(year) if year.isdigit() else None,
        "engine": clean(car.get("engine"), 40),
        "trim": clean(car.get("trim"), 40),
        "market": clean(car.get("market"), 20),
        "options": sorted({o for o in options if o in OPTIONS}),
        "lines": lines,
        "dropped_lines": dropped,
    }
