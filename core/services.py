"""Service procedures: step-by-step jobs like resets, calibrations and adaptations.

Each procedure is a JSON file in services/ listing its steps, so nothing runs that
you can't read first. Step types:

  {"type": "prompt",  "text": "Turn the steering wheel to centre"}         user confirms
  {"type": "session", "session": "0x03"}                                    diagnostic session
  {"type": "security","level": "0x01"}                                      security access (needs hook)
  {"type": "routine", "id": "0x0202", "action": "start", "data": "", "wait": 0}
  {"type": "results", "id": "0x0202", "poll": 10, "interval": 1.0, "done": "0x00"}
  {"type": "write",   "did": "0x4030", "data": "00"}                        write a DID
  {"type": "reset",   "reset_type": "0x01"}                                 ECU reset
  {"type": "wait",    "seconds": 5}
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .paths import definition_files, user_dir
from .uds import UDS, UdsError

SERVICES_DIR = user_dir("services")

ACTIONS = {"start": 0x01, "stop": 0x02, "results": 0x03}


@dataclass
class Step:
    type: str
    args: Dict


@dataclass
class Procedure:
    id: str
    name: str
    category: str
    description: str
    vehicle: str
    verified: bool
    source: str
    bus: str
    module: int
    module_label: str
    preconditions: List[str]
    steps: List[Step] = field(default_factory=list)
    path: str = ""


def _int(v) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


def load_all(folder: Optional[str] = None) -> List[Procedure]:
    out = []
    files = definition_files("services") if folder is None else [
        (fn, os.path.join(folder, fn)) for fn in sorted(os.listdir(folder))
        if fn.endswith(".json") and not fn.startswith("_")] if os.path.isdir(folder) else []
    for fn, path in files:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            steps = [Step(s["type"], s) for s in d.get("steps", [])]
            for s in steps:
                if s.type not in ("prompt", "session", "security", "routine", "results", "write", "reset", "wait"):
                    raise ValueError(f"unknown step type '{s.type}'")
            out.append(Procedure(
                d.get("id", fn[:-5]), d["name"], d.get("category", "General"), d.get("description", ""),
                d.get("vehicle", ""), bool(d.get("verified", False)), d.get("source", ""),
                d.get("bus", "HS").upper(), _int(d["module"]), d.get("module_label", ""),
                d.get("preconditions", []), steps, path))
        except (OSError, ValueError, KeyError) as e:
            print(f"Skipping service {fn}: {e}")
    return sorted(out, key=lambda p: (p.category, p.name))


def describe(step: Step) -> str:
    a = step.args
    t = step.type
    if t == "prompt":
        return a.get("text", "Confirm to continue")
    if t == "session":
        return f"Open diagnostic session {_int(a.get('session', 3)):02X}"
    if t == "security":
        return "Unlock module (security access)"
    if t == "routine":
        return f"Routine {_int(a['id']):04X} - {a.get('action', 'start')}"
    if t == "results":
        return f"Wait for routine {_int(a['id']):04X} to finish"
    if t == "write":
        return f"Write {_int(a['did']):04X} = {a['data']}"
    if t == "reset":
        return "Reset module"
    if t == "wait":
        return f"Wait {a.get('seconds', 1)} s"
    return t


def run_step(uds: UDS, p: Procedure, step: Step, log: Callable[[str], None] = print,
             cancelled: Callable[[], bool] = lambda: False) -> str:
    """Runs one non-prompt step on the adapter. Returns a short result text or raises UdsError."""
    a, tx = step.args, p.module
    if step.type == "session":
        uds.session(tx, _int(a.get("session", 3)))
        return "session open"
    if step.type == "security":
        uds.security_access(tx, _int(a.get("level", 1)))
        return "unlocked"
    if step.type == "routine":
        sub = ACTIONS.get(a.get("action", "start"), 0x01)
        res = uds.routine(tx, _int(a["id"]), sub, bytes.fromhex(a.get("data", "")))
        if a.get("wait"):
            time.sleep(float(a["wait"]))
        return f"OK {res.hex(' ').upper()}".strip()
    if step.type == "results":
        done = bytes.fromhex(str(a.get("done", "00")).lower().replace("0x", "").replace(" ", ""))
        polls, interval = int(a.get("poll", 10)), float(a.get("interval", 1.0))
        for i in range(polls):
            if cancelled():
                raise UdsError("Cancelled")
            res = uds.routine(tx, _int(a["id"]), 0x03)
            log(f"  routine status: {res.hex(' ').upper() or '-'}")
            if res[:len(done)] == done:
                return "finished"
            time.sleep(interval)
        raise UdsError("Routine didn't report finished in time")
    if step.type == "write":
        uds.write_did(tx, _int(a["did"]), bytes.fromhex(a["data"]))
        return "written"
    if step.type == "reset":
        uds.ecu_reset(tx, _int(a.get("reset_type", 1)))
        time.sleep(float(a.get("settle", 2)))
        return "module reset"
    if step.type == "wait":
        time.sleep(float(a.get("seconds", 1)))
        return "done"
    raise UdsError(f"Step type {step.type} must be handled by the UI")
