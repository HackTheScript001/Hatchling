"""Safety nets around writing to the car: a battery check before any change, a full backup of every
module before the first change of each visit, and putting modules back from a full backup or the
factory file.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from .asbuilt import BACKUP_DIR, MIN_WRITE_VOLTAGE, Blocks, read_blocks
from .elm import ElmError
from .modules import Module
from .uds import UDS, UdsError

WARN_BELOW = 12.4     # a resting battery under this is getting flat - writes can still go ahead
ENGINE_RUNNING = 13.3  # above this the alternator or a charger is supplying power

OK, LOW, TOO_LOW, UNKNOWN = "ok", "low", "too low", "unknown"


def battery_state(volts: Optional[float]) -> Tuple[str, str]:
    """(state, plain explanation) for the voltage the adapter reads at the OBD socket."""
    if volts is None:
        return UNKNOWN, ("The adapter couldn't read the battery voltage. Make sure the battery is charged, or "
                         "connect a charger, before changing anything.")
    if volts < MIN_WRITE_VOLTAGE:
        return TOO_LOW, (f"The battery is at <b>{volts:.1f} V</b>. That's too low to change module settings "
                         f"safely (Hatchling needs {MIN_WRITE_VOLTAGE} V or more). If the voltage drops while a "
                         "module is being written, the module can stop working.<br><br>Connect a battery charger "
                         "(or a jump pack) and try again.")
    if volts < WARN_BELOW:
        return LOW, (f"The battery is at <b>{volts:.1f} V</b>, which is getting low. A change only takes a few "
                     "seconds, but connecting a charger first is the safe choice - the modules and the ignition "
                     "draw power the whole time.")
    if volts >= ENGINE_RUNNING:
        return OK, f"Battery {volts:.1f} V - a charger or the engine is supplying power."
    return OK, f"Battery {volts:.1f} V."


# ---- full backups (every module, one file) ---------------------------------------
def read_modules(uds: UDS, mods: List[Module], log: Callable[[str], None] = print) -> List[Tuple[Module, Blocks]]:
    out = []
    for m in mods:
        try:
            blocks = read_blocks(uds, m)
        except (UdsError, ElmError) as e:
            log(f"{m.name}: not backed up - {e}")
            continue
        if blocks:
            out.append((m, blocks))
    return out


def save_full(vin: str, entries: List[Tuple[Module, Blocks]], note: str = "") -> str:
    folder = os.path.join(BACKUP_DIR, vin or "unknown-vin")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("full-backup-%Y%m%d-%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"kind": "full", "vin": vin, "taken": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note,
                   "modules": [{"addr": f"{m.addr:03X}", "bus": m.bus, "name": m.name,
                                "part": m.identity.get("Part number", ""),
                                "blocks": {f"{d:04X}": b.hex().upper() for d, b in sorted(blocks.items())}}
                               for m, blocks in entries]}, f, indent=1)
    return path


def load_full(path: str) -> Tuple[dict, List[Tuple[str, int, str, Blocks]]]:
    """(info, [(bus, addr, name, blocks)])."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if d.get("kind") != "full" or not isinstance(d.get("modules"), list):
        raise ValueError("that isn't a full backup")
    mods = []
    for e in d["modules"]:
        blocks = {int(k, 16): bytes.fromhex(v) for k, v in e.get("blocks", {}).items()}
        mods.append((e["bus"], int(e["addr"], 16), e.get("name", ""), blocks))
    return d, mods


def full_backups(vin: str) -> List[str]:
    folder = os.path.join(BACKUP_DIR, vin or "unknown-vin")
    if not os.path.isdir(folder):
        return []
    return sorted((os.path.join(folder, fn) for fn in os.listdir(folder) if fn.startswith("full-backup-")),
                  reverse=True)


def restore_targets(current: Blocks, wanted: Blocks) -> Blocks:
    """Blocks to write so `current` matches `wanted` - only blocks that differ and have the same length."""
    return {d: b for d, b in wanted.items() if d in current and len(current[d]) == len(b) and current[d] != b}
