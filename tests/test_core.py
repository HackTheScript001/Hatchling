"""Core tests against the simulator - no car, no internet. Run: python tests/test_core.py"""
import hashlib
import http.server
import os
import socket
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp()
os.environ["HOME"] = os.environ["USERPROFILE"] = HOME

from core import asbuilt, factory, finder, library, modules, problem, report, safety, trips, updates  # noqa: E402
from core.elm import ELM327, HS, MS, OBD, TcpTransport  # noqa: E402
from core.obd import GenericObd, misfire_counts, readiness_report  # noqa: E402
from core.sim import SimTransport, sample_factory  # noqa: E402
from core.uds import UDS  # noqa: E402

VIN = "WF0DXXGAKDFA12345"
failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# Wi-Fi transport against a fake adapter server backed by the simulator
sim = SimTransport()
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)


def serve():
    c, _ = srv.accept()
    buf = b""
    while True:
        d = c.recv(1024)
        if not d:
            break
        buf += d
        while b"\r" in buf:
            line, buf = buf.split(b"\r", 1)
            c.sendall(("\r".join(sim.send(line.decode())) + "\r\r>").encode())


threading.Thread(target=serve, daemon=True).start()
elm = ELM327(TcpTransport("127.0.0.1", srv.getsockname()[1]))
elm.initialise()
check("wi-fi adapter answers", elm.voltage() == 12.6)
uds = UDS(elm)

r = readiness_report(lambda pid: uds.obd_pid(pid, 0x7E0))
check("MOT readiness decodes", r["incomplete"] == ["Evaporative system"] and r["mil"] and not r["ready"])
check("misfire counts", misfire_counts(uds.mode06) == [(1, 0, 0), (2, 1, 0), (3, 14, 22), (4, 0, 1)])

hs = modules.scan(uds, HS, False, lambda a, b: None)
check("HS scan", {m.addr for m in hs} >= {0x726, 0x7E0, 0x760})
entries = safety.read_modules(uds, hs)
path = safety.save_full(VIN, entries)
info, saved = safety.load_full(path)
check("full backup round trip", len(saved) == len(entries) > 0)

fac = sample_factory()
elm.set_bus(MS)
bcm = next(m for m in modules.scan(uds, MS, False, lambda a, b: None) if m.addr == 0x726)
cur = asbuilt.read_blocks(uds, bcm)
check("changes since factory found", len(finder.compare(factory.align(fac.blocks(0x726), cur), cur)) == 1)
targets = safety.restore_targets(cur, factory.align(fac.blocks(0x726), cur))
res = asbuilt.safe_write(uds, bcm, cur, {**cur, **targets}, VIN, lambda s: None)
check("restore to factory writes and verifies", res.verified)
check("battery states", [safety.battery_state(v)[0] for v in (12.0, 12.3, 12.6, None)] ==
      ["too low", "low", "ok", "unknown"])

leg = ELM327(SimTransport(legacy=True))
leg.initialise()
leg.set_bus(OBD)
o = GenericObd(leg)
o.connect()
ff = o.freeze_frame()
check("OBD freeze frame", ff.get("Code that saved it") == "P0171" and "Engine speed" in ff)

check("health report builds", "MOT readiness" in report.build_html(VIN, 12.5, hs, [], "Ford", r, {"enabled": ["A"]}))

t = trips.Trip()
for i in range(61):
    t.add(i, 60, trips.fuel_lph(maf_gs=10))
check("trip maths", abs(t.km - 1.0) < 0.01 and 50 < t.mpg < 53)
run = trips.PerfRun("0-60 mph")
for i in range(5):
    run.add(i * 0.1, 0)
for i in range(1, 120):
    run.add(0.4 + i * 0.1, i * 1.0)
check("0-60 timer", run.result is not None and abs(run.result - 9.66) < 0.05, str(run.result))

rep = problem.build("it broke", [f"VIN {VIN}", r"C:\Users\Liam\Documents\x", "me@x.com"], "ELM", "Ford", [VIN])
check("problem report redacts", VIN not in rep["log"] and "Liam" not in rep["log"] and "me@x.com" not in rep["log"])

# updater: download and checksum (local server stands in for GitHub)
d = tempfile.mkdtemp()
exe = os.path.join(d, "Hatchling-Setup-9.9.9.exe")
with open(exe, "wb") as f:
    f.write(os.urandom(200000))
with open(exe, "rb") as f:
    sha = hashlib.sha256(f.read()).hexdigest()


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=d, **k)

    def log_message(self, *a):
        pass


hsrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=hsrv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{hsrv.server_address[1]}/"
updates.GITHUB_HOSTS = (base,)
p = updates.download(updates.Release("9.9.9", base + "Hatchling-Setup-9.9.9.exe", "", sha, 200000))
check("update download + checksum", os.path.getsize(p) == 200000)
try:
    updates.download(updates.Release("9.9.8", base + "Hatchling-Setup-9.9.9.exe", "", "0" * 64))
    check("bad checksum rejected", False)
except updates.UpdateError:
    check("bad checksum rejected", True)

notes = "Intro\n\nWhat's new in 2.1\n- **B** thing\n\nWhat's new in 2.0\n- A thing\n\nHow to install\nDownload `Hatchling-Setup-2.1.exe`"
wn = updates.whats_new(notes, "2.1", "1.9")
check("update notes: every newer version, no install text", "Version 2.1" in wn and "A thing" in wn and
      "Download" not in wn and "Intro" not in wn)
check("update notes: formatted", "<b>B</b>" in updates.notes_html(wn) and "**" not in updates.notes_html(wn))
# a connection that drops half way: the updater must carry on with a range request
blob = os.urandom(300000)
blob_sha = hashlib.sha256(blob).hexdigest()
calls = []


class Flaky(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rng = self.headers.get("Range")
        calls.append(rng)
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Length", str(len(blob) - start))
            self.end_headers()
            self.wfile.write(blob[start:])
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob[:120000])   # then hang up
            self.close_connection = True

    def log_message(self, *a):
        pass


fs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
threading.Thread(target=fs.serve_forever, daemon=True).start()
fbase = f"http://127.0.0.1:{fs.server_address[1]}/"
updates.GITHUB_HOSTS = (fbase,)
fp = updates.download(updates.Release("9.9.7", fbase + "Hatchling-Setup-9.9.7.exe", "", blob_sha, len(blob)))
with open(fp, "rb") as f:
    check("update resumes after a dropped connection", f.read() == blob and any(calls), str(calls))

# Linux update: unpack beside the running version, then switch 'current' over in one step
if sys.platform.startswith("linux"):
    import shutil
    import subprocess
    import core.paths as cpaths
    lhome = tempfile.mkdtemp()
    old = os.path.join(lhome, "versions", "1.0.0")
    shutil.copytree(ROOT, old, ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"))
    os.symlink(old, os.path.join(lhome, "current"))
    tdir = tempfile.mkdtemp()
    tball = os.path.join(tdir, "Hatchling-linux-9.9.9.tar.gz")
    subprocess.run(["tar", "-czf", tball, "--exclude=.git", "--transform", "s,^\\.,hatchling-9.9.9,", "-C", ROOT, "."],
                   check=True)
    real_home, real_app = updates.linux_home, cpaths.app_dir
    updates.linux_home = lambda: lhome
    cpaths.app_dir = lambda: os.path.join(lhome, "current")
    try:
        newdir = updates.prepare_linux(tball, "9.9.9")
        updates.install(newdir, relaunch=False)
        check("linux update unpacks and switches", os.path.realpath(os.path.join(lhome, "current")) == newdir and
              os.path.isfile(os.path.join(lhome, "current", "main.py")))
    finally:
        updates.linux_home, cpaths.app_dir = real_home, real_app

from core import garage  # noqa: E402

check("garage starts empty", garage.cars([(VIN, 5.0, 3)], {}) == [])
g = garage.add({}, VIN, "My Fiesta")
check("garage lists added cars", [c.name for c in garage.cars([(VIN, 5.0, 3)], g)] == ["My Fiesta"])

check("wish list loads", len(library.wishlist()) >= 5)

from core import vin as vinmod  # noqa: E402

check("non-Ford VINs get a make", vinmod.decode("W0L0XEP68G4012345").make == "Vauxhall/Opel" and
      not vinmod.decode("W0L0XEP68G4012345").ford_family)
check("Mazda is Ford family without a factory file",
      vinmod.decode("JMZBL14Z201234567").ford_family and not vinmod.decode("JMZBL14Z201234567").ford_factory)
mz = ELM327(SimTransport(kind="mazda"))
mz.initialise()
check("Mazda module names", "SAS - Airbags" in {m.name for m in modules.scan(UDS(mz), HS, False, None, "Mazda")})
other = ELM327(SimTransport(kind="other"))
other.initialise()
other.set_bus(OBD)
og = GenericObd(other)
og.connect()
check("any car: engine codes", og.stored_dtcs() == ["P0300"])
check("any car: misfire counts over generic OBD-II", len(misfire_counts(og.mode06)) == 4)

print(f"\n{len(failures)} failure(s)" if failures else "\nall core tests passed")
sys.exit(1 if failures else 0)
