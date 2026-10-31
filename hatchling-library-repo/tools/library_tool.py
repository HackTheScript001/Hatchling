"""Maintains the shared feature library in library/ (run by GitHub Actions, or by hand).

  python tools/library_tool.py add      <issue-body-file> <issue-number>   approved "Share a feature" issue
  python tools/library_tool.py confirm  <issue-body-file> <github-login>   "Works on my car" issue
  python tools/library_tool.py index                                       rebuild library/index.json
  python tools/library_tool.py check                                       validate every feature file

Issue bodies are untrusted input: everything is validated, only known fields are kept,
and nothing in them is ever executed. Feature files never contain a VIN.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "library")
FEATURES = os.path.join(LIB, "features")
INDEX = os.path.join(LIB, "index.json")
CONFIRMATIONS = os.path.join(LIB, "confirmations.json")

MAX_BODY = 64 * 1024
MAKES = {"Ford", "Lincoln", "Mercury", "Mazda", "Other"}
BUSES = {"HS", "MS", "HS2"}
CHECK_TYPES = {"manual", "module_present", "did_value"}
VIN_RE = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")
PART_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,39}$")
HEX4 = re.compile(r"^(0x)?[0-9A-Fa-f]{4}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


class Invalid(ValueError):
    pass


# ---- validation (same rules the app follows) -------------------------------------------
def _text(d: dict, key: str, limit: int, required: bool = False) -> str:
    v = d.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise Invalid(f"{key} must be text")
    v = v.strip()
    if required and not v:
        raise Invalid(f"{key} is required")
    if len(v) > limit:
        raise Invalid(f"{key} is longer than {limit} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise Invalid(f"{key} contains control characters")
    return v


def _int(v: Any, lo: int, hi: int, what: str) -> int:
    if isinstance(v, bool):
        raise Invalid(f"{what} isn't a number")
    try:
        n = int(v, 0) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        raise Invalid(f"{what} isn't a number")
    if not lo <= n <= hi:
        raise Invalid(f"{what} must be between {lo} and {hi}")
    return n


def _did(v: Any, what: str) -> str:
    if not isinstance(v, str) or not HEX4.match(v):
        raise Invalid(f"{what} must be 4 hex digits")
    return v[-4:].upper()


def validate_feature(d: Any) -> dict:
    """Clean copy with only known fields, or raises Invalid."""
    if not isinstance(d, dict):
        raise Invalid("the feature must be a JSON object")
    out = {
        "name": _text(d, "name", 80, True),
        "category": _text(d, "category", 40) or "General",
        "description": _text(d, "description", 1000),
        "vehicle": _text(d, "vehicle", 120),
        "learned_on": _text(d, "learned_on", 120),
        "hardware_required": _text(d, "hardware_required", 300),
        "source": _text(d, "source", 300),
        "verified": False,
    }
    mods = d.get("modules")
    if not isinstance(mods, list) or not 1 <= len(mods) <= 10:
        raise Invalid("a feature needs 1-10 modules")
    out["modules"], seen = [], set()
    for i, m in enumerate(mods):
        if not isinstance(m, dict):
            raise Invalid(f"module {i + 1} must be an object")
        bus = _text(m, "bus", 4, True).upper()
        if bus not in BUSES:
            raise Invalid(f"module {i + 1}: bus must be one of {sorted(BUSES)}")
        addr = _int(m.get("module"), 0x700, 0x7FF, f"module {i + 1} address")
        if (bus, addr) in seen:
            raise Invalid(f"module {addr:03X} on {bus} is listed twice")
        seen.add((bus, addr))
        entry = {"module": f"0x{addr:03X}", "bus": bus, "label": _text(m, "label", 60)}
        for key, lim in (("part", 40), ("software", 40)):
            v = _text(m, key, lim).upper()
            if v:
                if not PART_RE.match(v):
                    raise Invalid(f"module {i + 1}: {key} number looks wrong")
                entry[key] = v
        changes = m.get("changes")
        if not isinstance(changes, list) or not 1 <= len(changes) <= 32:
            raise Invalid(f"module {i + 1}: needs 1-32 changes")
        entry["changes"] = []
        for j, c in enumerate(changes):
            if not isinstance(c, dict):
                raise Invalid(f"module {i + 1} change {j + 1} must be an object")
            what = f"module {i + 1} change {j + 1}"
            mask = _int(c.get("mask"), 1, 255, f"{what} mask")
            on = _int(c.get("on"), 0, 255, f"{what} on")
            off = _int(c.get("off", 0), 0, 255, f"{what} off")
            if on & ~mask or off & ~mask:
                raise Invalid(f"{what}: on/off set bits outside the mask")
            if on == off:
                raise Invalid(f"{what}: on and off are the same")
            entry["changes"].append({"did": _did(c.get("did"), f"{what} block"),
                                     "byte": _int(c.get("byte"), 0, 63, f"{what} byte"),
                                     "mask": f"0x{mask:02X}", "on": f"0x{on:02X}", "off": f"0x{off:02X}"})
        out["modules"].append(entry)
    checks = d.get("hardware_checks", []) or []
    if not isinstance(checks, list) or len(checks) > 10:
        raise Invalid("hardware_checks must be a list of up to 10")
    out["hardware_checks"] = []
    for k, c in enumerate(checks):
        if not isinstance(c, dict):
            raise Invalid(f"hardware check {k + 1} must be an object")
        t = _text(c, "type", 20, True)
        if t not in CHECK_TYPES:
            raise Invalid(f"hardware check {k + 1}: unknown type")
        hc = {"type": t, "label": _text(c, "label", 120, True), "help": _text(c, "help", 300)}
        if t != "manual":
            bus = _text(c, "bus", 4, True).upper()
            if bus not in BUSES:
                raise Invalid(f"hardware check {k + 1}: bad bus")
            hc["bus"] = bus
            hc["module"] = f"0x{_int(c.get('module'), 0x700, 0x7FF, f'hardware check {k + 1} module'):03X}"
        if t == "did_value":
            hc["did"] = _did(c.get("did"), f"hardware check {k + 1} block")
            hc["byte"] = _int(c.get("byte"), 0, 63, f"hardware check {k + 1} byte")
            hc["mask"] = f"0x{_int(c.get('mask', 255), 1, 255, f'hardware check {k + 1} mask'):02X}"
            hc["value"] = f"0x{_int(c.get('value', 0), 0, 255, f'hardware check {k + 1} value'):02X}"
        out["hardware_checks"].append(hc)
    if VIN_RE.search(json.dumps(out).upper()):
        raise Invalid("the feature contains something that looks like a VIN - remove it before sharing")
    return out


def slug(name: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "feature")[:40]


def part_family(p: str) -> str:
    return p.rsplit("-", 1)[0].upper() if p.count("-") >= 2 else p.upper()


# ---- issue form parsing ------------------------------------------------------------------
def issue_fields(body: str) -> Dict[str, str]:
    """GitHub issue forms render as '### Field label' followed by the answer."""
    if len(body) > MAX_BODY:
        raise Invalid("issue is too long")
    fields, current, buf = {}, None, []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            if current:
                fields[current] = "\n".join(buf).strip()
            current, buf = line[4:].strip().lower(), []
        elif current:
            buf.append(line)
    if current:
        fields[current] = "\n".join(buf).strip()
    for k, v in list(fields.items()):
        if v == "_No response_":
            fields[k] = ""
    return fields


def _json_from(text: str) -> Any:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = (m.group(1) if m else text).strip()
    try:
        return json.loads(raw)
    except ValueError as e:
        raise Invalid(f"the feature file isn't valid JSON ({e.msg})")


# ---- library files -----------------------------------------------------------------------
def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def feature_files():
    for make in sorted(os.listdir(FEATURES)) if os.path.isdir(FEATURES) else []:
        folder = os.path.join(FEATURES, make)
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if fn.endswith(".json"):
                yield make, os.path.join(folder, fn)


def find(fid: str) -> Tuple[str, str]:
    for make, path in feature_files():
        if os.path.basename(path)[:-5] == fid:
            return make, path
    raise Invalid(f"there's no feature '{fid}' in the library")


def add(body: str, issue: int) -> Tuple[str, str]:
    f = issue_fields(body)
    make = f.get("make", "Ford").strip() or "Ford"
    if make not in MAKES:
        raise Invalid(f"make must be one of {sorted(MAKES)}")
    feature = validate_feature(_json_from(f.get("feature file", "")))
    fid = f"{slug(feature['name'])}-{int(issue)}"
    feature["library_id"] = fid
    feature["shared_in_issue"] = int(issue)
    path = os.path.join(FEATURES, make.lower(), f"{fid}.json")
    _write_json(path, feature)
    build_index()
    return fid, path


def confirm(body: str, login: str) -> Tuple[str, int]:
    if not LOGIN_RE.match(login or ""):
        raise Invalid("bad GitHub login")
    f = issue_fields(body)
    fid = f.get("feature id", "").strip()
    if not ID_RE.match(fid):
        raise Invalid("the feature id looks wrong")
    find(fid)
    part = f.get("module part number", "").strip().upper()
    if part and not PART_RE.match(part):
        raise Invalid("the part number looks wrong")
    data = _load_json(CONFIRMATIONS, {})
    data.setdefault(fid, {})[login.lower()] = part_family(part) if part else "unknown"
    _write_json(CONFIRMATIONS, data)
    build_index()
    return fid, len(data[fid])


def build_index() -> dict:
    conf = _load_json(CONFIRMATIONS, {})
    entries = []
    for make, path in feature_files():
        d = validate_feature(_load_json(path, None))   # every file must still pass
        fid = os.path.basename(path)[:-5]
        by: Dict[str, int] = {}
        for fam in conf.get(fid, {}).values():
            by[fam] = by.get(fam, 0) + 1
        entries.append({
            "id": fid, "name": d["name"], "make": make.capitalize() if make != "other" else "Other",
            "category": d["category"], "description": d["description"], "vehicle": d["vehicle"],
            "learned_on": d["learned_on"], "hardware_required": d["hardware_required"],
            "modules": [{k: m.get(k, "") for k in ("module", "bus", "label", "part", "software")}
                        for m in d["modules"]],
            "confirmations": sum(by.values()), "confirmations_by_family": by,
            "path": os.path.relpath(path, LIB).replace(os.sep, "/"),
            "updated": time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(path))),
        })
    index = {"schema": 1, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "features": entries}
    _write_json(INDEX, index)
    return index


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    try:
        if cmd == "add" and len(argv) == 3:
            with open(argv[1], encoding="utf-8") as fh:
                fid, path = add(fh.read(), int(argv[2]))
            print(f"added {fid} -> {os.path.relpath(path, ROOT)}")
        elif cmd == "confirm" and len(argv) == 3:
            with open(argv[1], encoding="utf-8") as fh:
                fid, n = confirm(fh.read(), argv[2])
            print(f"confirmed {fid} ({n} car(s))")
        elif cmd == "index":
            print(f"index: {len(build_index()['features'])} feature(s)")
        elif cmd == "check":
            for _, path in feature_files():
                validate_feature(_load_json(path, None))
            print("all feature files are valid")
        else:
            print(__doc__)
            return 2
    except Invalid as e:
        print(f"REJECTED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
