"""Diagnostic report export (HTML you can open, print or email)."""
from __future__ import annotations

import html
import os
import time
from .paths import user_dir
from typing import Dict, List, Tuple

REPORT_DIR = user_dir("reports")


def write_html(vin: str, voltage, modules, dtcs: List[Tuple[str, str, str, str]]) -> str:
    """modules: iterable of Module; dtcs: (module, code, status, description)."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M")
    path = os.path.join(REPORT_DIR, time.strftime(f"report-{vin or 'car'}-%Y%m%d-%H%M%S.html"))
    e = html.escape
    mod_rows = "".join(
        f"<tr><td>{m.addr:03X}</td><td>{m.bus}</td><td>{e(m.name)}</td><td>{e(m.identity.get('Part number', ''))}</td>"
        f"<td>{e(m.identity.get('Software', ''))}</td></tr>" for m in sorted(modules, key=lambda m: (m.bus, m.addr)))
    dtc_rows = "".join(f"<tr><td>{e(a)}</td><td><b>{e(b)}</b></td><td>{e(c)}</td><td>{e(d)}</td></tr>"
                       for a, b, c, d in dtcs) or "<tr><td colspan=4>No fault codes</td></tr>"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><meta charset="utf-8"><title>Hatchling report {e(vin)}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#1b2430}}h1{{margin:0}}
.m{{color:#667}}table{{border-collapse:collapse;width:100%;margin:10px 0 26px}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #dde3ea;font-size:14px}}th{{background:#f2f5f8}}</style>
<h1>Vehicle diagnostic report</h1>
<p class="m">VIN {e(vin or 'unknown')} · {ts} · battery {f'{voltage:.1f} V' if voltage else 'n/a'} · Hatchling Diagnostics</p>
<h2>Modules ({len(list(modules))})</h2>
<table><tr><th>Addr</th><th>Bus</th><th>Module</th><th>Part number</th><th>Software</th></tr>{mod_rows}</table>
<h2>Fault codes ({len(dtcs)})</h2>
<table><tr><th>Module</th><th>Code</th><th>Status</th><th>Description</th></tr>{dtc_rows}</table>""")
    return path
