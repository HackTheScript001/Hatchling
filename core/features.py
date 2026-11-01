"""Feature profiles: switch-style features made of per-module parts.

A feature (e.g. cruise control) usually needs bits set in several modules. Each
module is a "part" with its own on/off state, so you can switch the whole feature
or just one module. Every bit a part touches is listed in the JSON file.
See features/_TEMPLATE.json.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .paths import definition_files, user_dir
from .asbuilt import Blocks
from .modules import name_for

FEATURES_DIR = user_dir("features")

ModKey = Tuple[str, int]  # (bus, address)

# part / feature states
ENABLED, DISABLED, MIXED, UNKNOWN, NEEDS_VALUES = "enabled", "disabled", "partly on", "unknown", "needs values"
NOT_FOUND = "module not found"   # the car has no module at that address (e.g. option not fitted)


@dataclass
class Change:
    did: int
    byte: int
    mask: int
    on: int
    off: int


@dataclass
class Part:
    bus: str
    module: int
    label: str
    changes: List[Change] = field(default_factory=list)
    part_number: str = ""   # module the values were learned on (for compatibility checks)
    software: str = ""
    block_sizes: Dict[int, int] = field(default_factory=dict)   # block -> length on the car it was learned on

    @property
    def key(self) -> ModKey:
        return (self.bus, self.module)

    @property
    def configured(self) -> bool:
        return bool(self.changes) and all(c.mask for c in self.changes)


@dataclass
class HardwareCheck:
    """Something that must be true before the feature can be switched on.

    kind = "module_present": the module at bus/module must answer.
    kind = "did_value":      read `did` on the module, (byte & mask) must equal value.
    kind = "manual":         the user confirms it themselves (e.g. switches fitted).
    """
    kind: str
    label: str
    bus: str = "MS"
    module: int = 0
    did: int = 0
    byte: int = 0
    mask: int = 0xFF
    value: int = 0
    help: str = ""


@dataclass
class Feature:
    id: str
    name: str
    category: str
    description: str
    vehicle: str
    verified: bool
    source: str
    hardware_required: str
    parts: List[Part] = field(default_factory=list)
    checks: List[HardwareCheck] = field(default_factory=list)
    path: str = ""
    learned_on: str = ""     # e.g. "2015 Ford Fiesta" - never a full VIN
    shared: bool = False     # imported from someone else's car
    library_id: str = ""     # id in the shared feature library, if it came from there

    @property
    def configured(self) -> bool:
        return bool(self.parts) and all(p.configured for p in self.parts)


def _int(v) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


def _change(c) -> Change:
    return Change(int(c["did"], 16), _int(c["byte"]), _int(c.get("mask", 0)),
                  _int(c.get("on", 0)), _int(c.get("off", 0)))


def _parse(d: dict, fallback_id: str) -> Feature:
    parts: List[Part] = []
    if "modules" in d:
        for m in d["modules"]:
            bus, addr = m.get("bus", "MS").upper(), _int(m["module"])
            label = m.get("label") or name_for(bus, addr)
            sizes = {}
            for k, v in (m.get("blocks") or {}).items():
                try:
                    sizes[int(k, 16)] = int(v)
                except (TypeError, ValueError):
                    pass
            parts.append(Part(bus, addr, label, [_change(c) for c in m.get("changes", [])],
                              m.get("part", ""), m.get("software", ""), sizes))
    else:  # older flat "changes" format - group by module
        by: Dict[ModKey, Part] = {}
        for c in d.get("changes", []):
            bus, addr = c.get("bus", "MS").upper(), _int(c["module"])
            p = by.setdefault((bus, addr), Part(bus, addr, name_for(bus, addr)))
            p.changes.append(_change(c))
        parts = list(by.values())
    checks = []
    for c in d.get("hardware_checks", []):
        kind = c.get("type", "manual")
        if kind not in ("module_present", "did_value", "manual"):
            raise ValueError(f"unknown hardware check type '{kind}'")
        checks.append(HardwareCheck(
            kind, c.get("label", kind), c.get("bus", "MS").upper(), _int(c.get("module", 0)),
            int(c.get("did", "0"), 16), _int(c.get("byte", 0)), _int(c.get("mask", 0xFF)),
            _int(c.get("value", 0)), c.get("help", "")))
    return Feature(d.get("id", fallback_id), d["name"], d.get("category", "General"), d.get("description", ""),
                   d.get("vehicle", ""), bool(d.get("verified", False)), d.get("source", ""),
                   d.get("hardware_required", ""), parts, checks,
                   path="", learned_on=d.get("learned_on", ""), shared=bool(d.get("imported_from")),
                   library_id=d.get("library_id", ""))


def load_all(folder: Optional[str] = None) -> List[Feature]:
    out = []
    files = definition_files("features") if folder is None else [
        (fn, os.path.join(folder, fn)) for fn in sorted(os.listdir(folder))
        if fn.endswith(".json") and not fn.startswith("_")] if os.path.isdir(folder) else []
    for fn, path in files:
        try:
            with open(path, encoding="utf-8") as f:
                feat = _parse(json.load(f), fn[:-5])
            feat.path = path
            out.append(feat)
        except (OSError, ValueError, KeyError) as e:
            print(f"Skipping feature {fn}: {e}")
    return sorted(out, key=lambda f: (f.category, f.name))


def part_state(p: Part, blocks: Blocks) -> str:
    if not p.configured:
        return NEEDS_VALUES
    results = []
    for c in p.changes:
        data = blocks.get(c.did) if blocks else None
        if data is None or c.byte >= len(data):
            return UNKNOWN
        v = data[c.byte] & c.mask
        results.append("on" if v == (c.on & c.mask) else "off" if v == (c.off & c.mask) else "other")
    if all(r == "on" for r in results):
        return ENABLED
    if all(r == "off" for r in results):
        return DISABLED
    return MIXED


def feature_state(part_states: List[str]) -> str:
    if not part_states:
        return UNKNOWN
    if NEEDS_VALUES in part_states:
        return NEEDS_VALUES
    if NOT_FOUND in part_states:
        return NOT_FOUND
    if UNKNOWN in part_states:
        return UNKNOWN
    if all(s == ENABLED for s in part_states):
        return ENABLED
    if all(s == DISABLED for s in part_states):
        return DISABLED
    return MIXED


def apply_part(p: Part, blocks: Blocks, enable: bool) -> Blocks:
    """New block set for one module with this part switched on/off (does not write)."""
    out = dict(blocks)
    for c in p.changes:
        data = bytearray(out[c.did])
        val = c.on if enable else c.off
        data[c.byte] = (data[c.byte] & ~c.mask & 0xFF) | (val & c.mask)
        out[c.did] = bytes(data)
    return out


def plan(f: Feature, current: Dict[ModKey, Blocks], wanted: Dict[ModKey, bool]) -> Dict[ModKey, Blocks]:
    """Target blocks for every module whose wanted state changes something."""
    targets: Dict[ModKey, Blocks] = {}
    for p in f.parts:
        if p.key not in wanted:
            continue
        base = targets.get(p.key, current[p.key])
        new = apply_part(p, base, wanted[p.key])
        if new != current[p.key]:
            targets[p.key] = new
    return targets


def run_check(uds, c: HardwareCheck) -> Tuple[bool, str]:
    """Runs a module_present / did_value check. Adapter must already be on c.bus."""
    from .uds import UdsError
    if c.kind == "module_present":
        ok = uds.tester_present(c.module)
        return ok, "found" if ok else f"no answer from {c.module:03X} on {c.bus}-CAN"
    if c.kind == "did_value":
        try:
            data = uds.read_did(c.module, c.did)
        except UdsError as e:
            return False, str(e)
        if c.byte >= len(data):
            return False, f"{c.did:04X} is shorter than expected"
        got = data[c.byte] & c.mask
        ok = got == (c.value & c.mask)
        return ok, f"read {got:02X}" + ("" if ok else f", expected {c.value & c.mask:02X}")
    raise ValueError("manual checks are answered by the user")


def remaining_parts(f: Feature, states: Dict[ModKey, str]) -> List[Part]:
    """Parts not yet enabled, in file order."""
    return [p for p in f.parts if states.get(p.key) != ENABLED]


def needed_modules(feats: List[Feature]) -> set:
    """(bus, address) of every module a configured feature reads."""
    return {p.key for f in feats if f.configured for p in f.parts}


def detect_all(feats: List[Feature], blocks: Dict[ModKey, Blocks], present: set) -> Dict[str, Dict[ModKey, str]]:
    """Part states for every configured feature after a scan.

    present = (bus, address) of modules the scan found; blocks = their As-Built data.
    A part whose module isn't on the car is NOT_FOUND, one whose blocks couldn't be read is UNKNOWN."""
    out = {}
    for f in feats:
        if not f.configured:
            continue
        out[f.id] = {p.key: (part_state(p, blocks[p.key]) if p.key in blocks
                             else NOT_FOUND if p.key not in present else UNKNOWN) for p in f.parts}
    return out


def summary(feats: List[Feature], states: Dict[str, Dict[ModKey, str]]) -> Dict[str, List[str]]:
    """Feature names grouped by overall state."""
    out: Dict[str, List[str]] = {}
    for f in feats:
        if f.id in states:
            out.setdefault(feature_state(list(states[f.id].values())), []).append(f.name)
    return out


# ---- compatibility with the connected car ------------------------------------------
SAME, FAMILY, DIFFERENT, UNKNOWN_PART, MISSING = "same", "same family", "different", "unknown", "missing"


def part_family(part_number: str) -> str:
    """Ford part numbers end in a revision suffix (C1BT-14A073-AE): the rest is the module family."""
    return part_number.rsplit("-", 1)[0].upper() if part_number.count("-") >= 2 else part_number.upper()


def compatibility(f: Feature, scanned: Dict[ModKey, dict]) -> List[Tuple[Part, str, str]]:
    """For each module the feature uses: how the car's module compares with the one it was learned on.
    scanned maps (bus, addr) -> identity dict from the scan ("Part number", "Software")."""
    out = []
    for p in f.parts:
        ident = scanned.get(p.key)
        if ident is None:
            out.append((p, MISSING, "module not found on this car"))
            continue
        mine = ident.get("Part number", "")
        if not p.part_number or not mine:
            out.append((p, UNKNOWN_PART, "no part number to compare"))
        elif mine.upper() == p.part_number.upper():
            out.append((p, SAME, mine))
        elif part_family(mine) == part_family(p.part_number):
            out.append((p, FAMILY, f"{mine} (learned on {p.part_number})"))
        else:
            out.append((p, DIFFERENT, f"{mine} (learned on {p.part_number})"))
    return out


def compatibility_level(results: List[Tuple[Part, str, str]]) -> str:
    states = [r[1] for r in results]
    if not states:
        return UNKNOWN_PART
    for level in (MISSING, DIFFERENT, UNKNOWN_PART, FAMILY):
        if level in states:
            return level
    return SAME


# ---- "Will this work on my car?" - checked before every write ------------------------------
PROVEN, LIKELY, UNTESTED, WONT = "proven", "likely", "untested", "won't work"
VERDICT_TEXT = {
    PROVEN: "Proven on your modules",
    LIKELY: "Very likely to work",
    UNTESTED: "Untested on your modules",
    WONT: "Won't work on your car",
}


@dataclass
class Verdict:
    level: str
    reasons: List[str]

    @property
    def headline(self) -> str:
        return VERDICT_TEXT[self.level]

    @property
    def blocked(self) -> bool:
        """WONT always stops a write; UNTESTED needs the owner to accept the risk."""
        return self.level == WONT


def write_verdict(f: Feature, scanned: Dict[ModKey, dict], blocks: Optional[Dict[ModKey, Blocks]] = None,
                  parts: Optional[List[Part]] = None, confirmations_by_family: Optional[Dict[str, int]] = None
                  ) -> Verdict:
    """Whether a feature's values are safe to write to *this* car.

    - every module it needs must be on the car
    - the settings blocks must be laid out the same (same length as where it was learned) - else it won't work
    - each setting must currently be one of the feature's own values (its 'on' or 'off') - anything else means
      the bits mean something different on this car
    - part numbers: same module -> likely (proven once verified or confirmed by other owners);
      same family -> likely; different or unknown -> untested
    blocks: what the car's modules hold now (read just before writing)."""
    parts = parts if parts is not None else f.parts
    blocks = blocks or {}
    reasons: List[str] = []
    wont = False
    for p in parts:
        if p.key not in scanned:
            wont = True
            reasons.append(f"{p.label}: this module isn't on your car")
            continue
        cur = blocks.get(p.key)
        if cur is None:
            continue
        for c in p.changes:
            data = cur.get(c.did)
            if data is None:
                wont = True
                reasons.append(f"{p.label}: the module doesn't have settings block {c.did:04X}")
                continue
            want = p.block_sizes.get(c.did)
            if want and len(data) != want:
                wont = True
                reasons.append(f"{p.label}: its settings are laid out differently ({len(data)} bytes in block "
                               f"{c.did:04X}, the car it was learned on has {want})")
                continue
            if c.byte >= len(data):
                wont = True
                reasons.append(f"{p.label}: block {c.did:04X} is too short for this setting")
                continue
            now = data[c.byte] & c.mask
            if now not in (c.on & c.mask, c.off & c.mask):
                wont = True
                reasons.append(f"{p.label}: the setting is currently {now:02X}, which is neither this feature's "
                               f"'on' ({c.on & c.mask:02X}) nor 'off' ({c.off & c.mask:02X}) - it means something "
                               "else on your car")
    if wont:
        return Verdict(WONT, reasons)
    res = compatibility(Feature(f.id, f.name, f.category, f.description, f.vehicle, f.verified, f.source,
                                f.hardware_required, parts), scanned)
    level = compatibility_level(res)
    confirms = 0
    for p, st, _ in res:
        mine = (scanned.get(p.key) or {}).get("Part number", "")
        if mine and confirmations_by_family:
            confirms += confirmations_by_family.get(part_family(mine), 0)
    for p, st, detail in res:
        if st == DIFFERENT:
            reasons.append(f"{p.label}: your module is {detail}")
        elif st == UNKNOWN_PART:
            reasons.append(f"{p.label}: {detail}")
    if level in (DIFFERENT, UNKNOWN_PART):
        return Verdict(UNTESTED, reasons or ["No part numbers to compare with the car it was learned on"])
    if level == SAME and (f.verified or confirms):
        why = "Your modules are the same parts it was learned on"
        why += f", and {confirms} other owner(s) with them say it works." if confirms else " and it's been tested."
        return Verdict(PROVEN, [why])
    if level == SAME:
        return Verdict(LIKELY, ["Your modules are the same parts it was learned on - not marked as tested yet."])
    return Verdict(LIKELY, ["Same type of module, a different version from the car it was learned on."]
                   + [f"{p.label}: {d}" for p, st, d in res if st == FAMILY])
