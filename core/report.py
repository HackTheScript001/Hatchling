"""Car health report: modules, fault codes, MOT readiness and features, as HTML (and PDF from the app)."""
from __future__ import annotations

import html
import os
import time
from typing import Dict, List, Optional, Tuple

from .paths import user_dir
from .version import VERSION

REPORT_DIR = user_dir("reports")


def build_html(vin: str, voltage, modules, dtcs: List[Tuple[str, str, str, str]], car: str = "",
               readiness: Optional[dict] = None, feature_groups: Optional[Dict[str, List[str]]] = None,
               for_pdf: bool = False) -> str:
    """modules: iterable of Module; dtcs: (module, code, status, description)."""
    e = html.escape
    ts = time.strftime("%d %B %Y, %H:%M")
    modules = sorted(modules, key=lambda m: (m.bus, m.addr))
    mod_rows = "".join(
        f"<tr><td>{m.addr:03X}</td><td>{m.bus}</td><td>{e(m.name)}</td><td>{e(m.identity.get('Part number', ''))}</td>"
        f"<td>{e(m.identity.get('Software', ''))}</td></tr>" for m in modules)
    dtc_rows = "".join(f"<tr><td>{e(a)}</td><td><b>{e(b)}</b></td><td>{e(c)}</td><td>{e(d)}</td></tr>"
                       for a, b, c, d in dtcs) or "<tr><td colspan=4>No fault codes stored</td></tr>"
    parts = []
    if readiness:
        verdict = "Looks ready for the MOT emissions check" if readiness.get("ready") else "Not ready yet"
        rows = "".join(f"<tr><td>{e(n)}</td><td>{'Complete' if c else 'Not finished yet'}</td></tr>"
                       for n, sup, c in readiness.get("monitors", []) if sup)
        parts.append(f"<h2>MOT readiness</h2><p><b>{verdict}</b> · engine warning light "
                     f"{'ON' if readiness.get('mil') else 'off'}</p><table><tr><th>Self-test</th><th>Result</th></tr>"
                     f"{rows}</table>")
    if feature_groups:
        names = {"enabled": "On", "partly on": "Partly on", "disabled": "Off"}
        lines = "".join(f"<tr><td>{names.get(k, k)}</td><td>{e(', '.join(v))}</td></tr>"
                        for k, v in feature_groups.items() if v and k in names)
        if lines:
            parts.append(f"<h2>Features</h2><table>{lines}</table>")
    style = "" if for_pdf else """<style>body{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#1b2430}
h1{margin:0}.m{color:#667}table{border-collapse:collapse;width:100%;margin:10px 0 26px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #dde3ea;font-size:14px}th{background:#f2f5f8}</style>"""
    table_attr = " cellpadding=5 cellspacing=0 border=1 width='100%'" if for_pdf else ""
    body = f"""<!doctype html><meta charset="utf-8"><title>Car health report {e(vin)}</title>{style}
<h1>Car health report</h1>
<p class="m">{e(car) + ' · ' if car else ''}VIN {e(vin or 'unknown')}<br>{ts} · battery {f'{voltage:.1f} V' if voltage else 'not read'}
· made with Hatchling {VERSION}</p>
<h2>Fault codes ({len(dtcs)})</h2>
<table><tr><th>Module</th><th>Code</th><th>Status</th><th>Meaning</th></tr>{dtc_rows}</table>
{''.join(parts)}
<h2>Modules ({len(modules)})</h2>
<table><tr><th>Addr</th><th>Bus</th><th>Module</th><th>Part number</th><th>Software</th></tr>{mod_rows}</table>"""
    return body.replace("<table>", f"<table{table_attr}>")


def write_html(vin: str, voltage, modules, dtcs, **kw) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, time.strftime(f"report-{vin or 'car'}-%Y%m%d-%H%M%S.html"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_html(vin, voltage, modules, dtcs, **kw))
    return path
