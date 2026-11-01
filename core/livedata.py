"""Live values from any module (UDS 22 reads), defined in livedata/*.json, plus CSV recording.

Definition example:
  {"module": "0x726", "bus": "MS", "did": "0x4028", "name": "Battery charge",
   "unit": "%", "formula": "A", "lo": 0, "hi": 100}

Formula letters A, B, C, D... are the response bytes. Allowed: + - * / // % ( ) numbers,
and the helpers signed8(X), signed16(X, Y), bit(X, n).
"""
from __future__ import annotations

import ast
import csv
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .paths import definition_files, user_dir
from .pids import PIDS

LIVEDATA_DIR = user_dir("livedata")
LOG_DIR = user_dir("logs")

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load, ast.Call,
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd,
            ast.BitAnd, ast.BitOr, ast.RShift, ast.LShift)
_HELPERS = {
    "signed8": lambda x: x - 256 if x > 127 else x,
    "signed16": lambda x, y: ((x << 8) | y) - 65536 if x > 127 else (x << 8) | y,
    "bit": lambda x, n: (x >> n) & 1,
}


def compile_formula(expr: str):
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"'{expr}': {type(node).__name__} not allowed")
        if isinstance(node, ast.Name) and not (node.id in _HELPERS or (len(node.id) == 1 and node.id.isupper())):
            raise ValueError(f"'{expr}': unknown name {node.id}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _HELPERS):
            raise ValueError(f"'{expr}': only signed8/signed16/bit calls allowed")
    code = compile(tree, "<formula>", "eval")

    def run(data: bytes) -> float:
        env = dict(_HELPERS)
        env.update({chr(65 + i): b for i, b in enumerate(data)})
        return float(eval(code, {"__builtins__": {}}, env))
    return run


@dataclass
class Signal:
    key: str
    name: str
    unit: str
    bus: str
    module: int
    kind: str          # "obd" (mode 01 PID) or "did" (UDS 22)
    ident: int
    lo: float
    hi: float
    formula: object

    @property
    def group(self) -> str:
        return "Engine (OBD-II)" if self.kind == "obd" else f"{self.bus} {self.module:03X}"


def obd_signals() -> List[Signal]:
    return [Signal(f"obd:{p.pid:02X}", p.name, p.unit, "HS", 0x7E0, "obd", p.pid, p.lo, p.hi, p.formula)
            for p in PIDS]


def load_all(folder: Optional[str] = None) -> List[Signal]:
    out = obd_signals()
    files = definition_files("livedata") if folder is None else [
        (fn, os.path.join(folder, fn)) for fn in sorted(os.listdir(folder))
        if fn.endswith(".json") and not fn.startswith("_")] if os.path.isdir(folder) else []
    for fn, path in files:
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)
            for d in items:
                bus, mod, did = d.get("bus", "HS").upper(), int(d["module"], 0), int(d["did"], 0)
                out.append(Signal(f"{bus}:{mod:03X}:{did:04X}", d["name"], d.get("unit", ""), bus, mod, "did",
                                  did, float(d.get("lo", 0)), float(d.get("hi", 100)),
                                  compile_formula(d.get("formula", "A"))))
        except (OSError, ValueError, KeyError) as e:
            print(f"Skipping live data file {fn}: {e}")
    return out


def read(uds, sig: Signal) -> Optional[float]:
    data = uds.obd_pid(sig.ident, sig.module) if sig.kind == "obd" else uds.read_did(sig.module, sig.ident)
    return sig.formula(data)


class Recorder:
    def __init__(self, signals: List[Signal]):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, time.strftime("live-%Y%m%d-%H%M%S.csv"))
        self.f = open(self.path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.keys = [s.key for s in signals]
        self.w.writerow(["time_s"] + [f"{s.name} ({s.unit})" for s in signals])
        self.t0 = time.monotonic()
        self.rows = 0

    def add(self, values: Dict[str, Optional[float]]):
        self.w.writerow([f"{time.monotonic() - self.t0:.2f}"] +
                        ["" if values.get(k) is None else f"{values[k]:.3f}" for k in self.keys])
        self.rows += 1

    def close(self):
        self.f.close()


def load_recording(path: str, max_rows: int = 200_000) -> tuple:
    """Reads a recording made by Recorder: (times, {column name: [value or None]})."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = csv.reader(f)
        head = next(rows, None)
        if not head or head[0] != "time_s":
            raise ValueError("that isn't a Hatchling recording")
        names = head[1:]
        times: List[float] = []
        cols: Dict[str, List[Optional[float]]] = {n: [] for n in names}
        for i, row in enumerate(rows):
            if i >= max_rows:
                break
            try:
                times.append(float(row[0]))
            except (ValueError, IndexError):
                continue
            for n, v in zip(names, row[1:] + [""] * (len(names) - len(row) + 1)):
                try:
                    cols[n].append(float(v) if v != "" else None)
                except ValueError:
                    cols[n].append(None)
    return times, cols
