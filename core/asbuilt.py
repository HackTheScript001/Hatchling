"""As-Built (module configuration) read / backup / diff / safe write."""
from __future__ import annotations

import json
import re
import os
import time
from dataclasses import dataclass
from .paths import user_dir
from typing import Callable, Dict, List, Optional, Tuple

from .modules import Module
from .uds import EXTENDED_SESSION, DEFAULT_SESSION, NegativeResponse, UDS, UdsError

AS_BUILT_RANGE = range(0xDE00, 0xDE20)
MIN_WRITE_VOLTAGE = 12.2
BACKUP_DIR = user_dir("backups")

Blocks = Dict[int, bytes]


def did_name(did: int) -> str:
    return f"{did:04X}"


def read_blocks(uds: UDS, m: Module, progress: Optional[Callable[[int, int], None]] = None) -> Blocks:
    blocks: Blocks = {}
    dids = list(AS_BUILT_RANGE)
    for i, did in enumerate(dids):
        if progress:
            progress(i + 1, len(dids))
        try:
            blocks[did] = uds.read_did(m.addr, did)
        except NegativeResponse:
            continue
    return blocks


def save_backup(m: Module, blocks: Blocks, vin: str = "", note: str = "") -> str:
    folder = os.path.join(BACKUP_DIR, vin or "unknown-vin")
    os.makedirs(folder, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(folder, f"{m.addr:03X}_{m.bus}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "app": "Hatchling", "format": 1, "saved": ts, "note": note, "vin": vin,
            "module": {"addr": f"{m.addr:03X}", "bus": m.bus, "name": m.name, "identity": m.identity},
            "blocks": {did_name(d): v.hex().upper() for d, v in sorted(blocks.items())},
        }, f, indent=2)
    return path


def load_backup(path: str) -> Tuple[dict, Blocks]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    blocks = {int(k, 16): bytes.fromhex(v) for k, v in data["blocks"].items()}
    return data, blocks


def diff(old: Blocks, new: Blocks) -> List[Tuple[int, int, int, int]]:
    """List of (did, byte_index, old, new) for every changed byte."""
    out = []
    for did, nb in new.items():
        ob = old.get(did)
        if ob is None:
            continue
        for i, (a, b) in enumerate(zip(ob, nb)):
            if a != b:
                out.append((did, i, a, b))
    return out


def format_words(data: bytes) -> str:
    """Ford-style display: 4 hex digits per group."""
    h = data.hex().upper()
    return " ".join(h[i:i + 4] for i in range(0, len(h), 4))


@dataclass
class WriteResult:
    backup_path: str
    written: List[int]
    verified: bool
    message: str


def safe_write(uds: UDS, m: Module, current: Blocks, target: Blocks, vin: str = "",
               log: Callable[[str], None] = print, use_security: bool = False) -> WriteResult:
    """Backup -> voltage check -> extended session -> write changed DIDs -> read back -> verify."""
    changed = [d for d in target if d in current and target[d] != current[d]]
    if not changed:
        return WriteResult("", [], True, "Nothing to write")
    for d in changed:
        if len(target[d]) != len(current[d]):
            raise UdsError(f"Block {did_name(d)} length changed - refusing to write")

    v = uds.elm.voltage()
    if v is not None and v < MIN_WRITE_VOLTAGE:
        raise UdsError(f"Battery at {v:.1f} V - connect a charger (need {MIN_WRITE_VOLTAGE} V+)")

    backup = save_backup(m, current, vin, note="automatic backup before write")
    log(f"Backup saved: {backup}")

    uds.session(m.addr, EXTENDED_SESSION)
    if use_security:
        uds.security_access(m.addr, 0x01)
    written = []
    try:
        for d in changed:
            log(f"Writing {did_name(d)}: {format_words(current[d])} -> {format_words(target[d])}")
            uds.write_did(m.addr, d, target[d])
            written.append(d)
    finally:
        try:
            uds.session(m.addr, DEFAULT_SESSION)
        except UdsError:
            pass

    bad = []
    for d in written:
        if uds.read_did(m.addr, d) != target[d]:
            bad.append(did_name(d))
    if bad:
        return WriteResult(backup, written, False,
                           f"Verify FAILED for {', '.join(bad)} - restore from backup")
    return WriteResult(backup, written, True,
                       f"Wrote and verified {len(written)} block(s). Cycle the ignition to apply.")


# ---- As-Built text import -------------------------------------------------
_LINE = re.compile(r"\b([0-9A-Fa-f]{3})-(\d{2})-(\d{2})\b[ \t:=]*([0-9A-Fa-f][0-9A-Fa-f \t]*)")


@dataclass
class ImportResult:
    modules: Dict[int, Blocks]
    lines: int
    checksum_matches: int


def parse_text(text: str, drop_last_byte: bool = True) -> ImportResult:
    """Parses As-Built lines written as  <module>-<block>-<line>  <hex...>,
    e.g. '726-01-01 2A80 40A6 CB'. Block 01 maps to DE00, 02 to DE01, and so on.
    Many sources end each line with a checksum byte; drop_last_byte removes it.
    The checksum test (sum of address, block, line and data bytes) is informational only."""
    raw: Dict[int, Dict[int, Dict[int, bytes]]] = {}
    lines = matches = 0
    for m in _LINE.finditer(text):
        addr, block, line = int(m.group(1), 16), int(m.group(2)), int(m.group(3))
        hexs = re.sub(r"\s+", "", m.group(4))
        if len(hexs) % 2 or block < 1:
            continue
        data = bytes.fromhex(hexs)
        if drop_last_byte and len(data) > 1:
            body, cks = data[:-1], data[-1]
            if (sum(body) + (addr >> 8) + (addr & 0xFF) + block + line) & 0xFF == cks:
                matches += 1
            data = body
        raw.setdefault(addr, {}).setdefault(0xDE00 + block - 1, {})[line] = data
        lines += 1
    modules = {a: {did: b"".join(parts[k] for k in sorted(parts)) for did, parts in blocks.items()}
               for a, blocks in raw.items()}
    return ImportResult(modules, lines, matches)
