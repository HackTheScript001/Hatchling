"""Clicks through every Hatchling screen against the simulator with no window shown.

Run: QT_QPA_PLATFORM=offscreen python tests/test_ui.py
Fails (exit 1) on any crash, unexpected error in the activity log or error box. GitHub Actions runs it
before every build, so a release never goes out with a screen that crashes.
"""
import os
import sys
import tempfile
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp()
os.environ["HOME"] = os.environ["USERPROFILE"] = HOME

from ui import theme  # noqa: E402

theme.apply(False)

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox  # noqa: E402

from core import library, online, updates  # noqa: E402
import ui.app as A  # noqa: E402

problems, opened, urls, boxes = [], [], [], []
current_step = ["start"]


def hook(t, v, tb):
    problems.append(f"[{current_step[0]}] crash: " + "".join(traceback.format_exception(t, v, tb)))


sys.excepthook = hook

# ---- nothing may block or reach the internet ----------------------------------------
def _question(parent, title, text, buttons=QMessageBox.Yes | QMessageBox.No, *a, **k):
    boxes.append(("question", title))
    return QMessageBox.Ok if buttons & QMessageBox.Ok else QMessageBox.Yes


def _note(kind):
    def f(parent, title, text, *a, **k):
        boxes.append((kind, title, str(text)[:200]))
        if kind == "critical":
            problems.append(f"[{current_step[0]}] error box: {title}: {str(text)[:300]}")
        return QMessageBox.Ok
    return f


QMessageBox.question = staticmethod(_question)
QMessageBox.information = staticmethod(_note("information"))
QMessageBox.warning = staticmethod(_note("warning"))
QMessageBox.critical = staticmethod(_note("critical"))
QMessageBox.exec = lambda self: QMessageBox.Ok
QDialog.exec = lambda self: (opened.append(type(self).__name__), 0)[1]
REPORT = os.path.join(HOME, "report.pdf")
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (REPORT, ""))
QInputDialog.getText = staticmethod(lambda *a, **k: ("Test car", True))
A.open_url = lambda url: urls.append(url)
updates.check = lambda *a, **k: None


def offline(*a, **k):
    raise library.LibraryError("offline (test)")


library.update = offline
library.update_extras = lambda *a, **k: []
online.community_posts = lambda *a, **k: []
online.vin_details = lambda *a, **k: {}

app = QApplication(sys.argv)
app.setStyleSheet(theme.STYLE)


def pump(seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, timeout=20.0, what=""):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    problems.append(f"[{current_step[0]}] timed out waiting for {what or 'condition'}")
    return False


def idle(timeout=30.0):
    pump(0.1)
    wait(lambda: not s.runner.busy and not s.net.busy, timeout, "the adapter to finish")
    pump(0.2)


def step(name):
    current_step[0] = name
    print(f"- {name}", flush=True)


step("open the main window")
w = A.MainWindow()
w.show()
s = w.s
s.logged.connect(lambda line: problems.append(f"[{current_step[0]}] log: {line}")
                 if ("ERROR:" in line or "UNEXPECTED" in line) else None)
pump(1.2)   # setup guide opens itself on first run
check_guide = w.setup_guide is not None and w.setup_guide.isVisible()
if not check_guide:
    problems.append("[start] setup guide didn't open on first run")

step("visit every page while disconnected")
for key in list(w.pages):
    w.go(key)
    pump(0.05)

step("setup guide with the simulator")
g = w.setup_guide
g.goto(0)
g.ports.setCurrentIndex(g.ports.findData(A.SIM))
g.do()                        # choose adapter
g.do()                        # connect
wait(lambda: s.connected, 20, "connect")
idle()
g.goto(1)
g.do()                        # -> scan step
g.do()                        # scan
wait(lambda: bool(s.modules), 60, "module scan")
idle(60)
g.goto(2)
g.do()                        # -> factory step (simulator has a sample factory file)
if not s.factory:
    problems.append("[setup] simulator factory file missing")
g.goto(3)
g.do()                        # -> backup step
g.do()                        # back up now
wait(lambda: s.vin in s.full_backup_done, 60, "full backup")
idle()
g.goto(4)
g.do()                        # finish
pump(0.2)

step("modules page")
mp = w.pages["Modules"]
w.go("Modules")
mp.render()
mp.show_factory()
mp.changes_since_factory()
idle()
if "FeatureFinderDialog" not in opened:
    problems.append("[modules] changes since factory didn't open the finder")

step("fault codes, details, MOT check, misfires")
dp = w.pages["Fault codes"]
w.go("Fault codes")
dp.read_all()
idle(60)
if dp.tbl.rowCount() == 0:
    problems.append("[codes] no fault codes read from the simulator")
for r in range(dp.tbl.rowCount()):
    dp.tbl.selectRow(r)
    dp.show_details()
    idle()
dp.mot_check()
idle()
dp.misfire_check()
idle()
for name in ("CodeDialog", "MotDialog", "MisfireDialog"):
    if name not in opened:
        problems.append(f"[codes] {name} didn't open")

step("health report PDF")
dp.export_report()
idle()
if not (os.path.isfile(REPORT) and os.path.getsize(REPORT) > 1000):
    problems.append("[report] the PDF wasn't written")

step("live data and replay")
lp = w.pages["Live data"]
w.go("Live data")
lp.toggle()
wait(lambda: lp.timer.isActive(), 10, "live data start")
lp.toggle_record()
pump(2.0)
rec = lp.recorder.path if lp.recorder else None
lp.stop()
idle()
if rec:
    times, cols = A.livedata.load_recording(rec)
    d = A.ReplayDialog(w, "test", times or [0, 1], cols or {"x": [1, 2]})
    d.chart.resize(600, 300)
    d.chart.grab()
    d.on_cursor(0)
else:
    problems.append("[live] recording didn't start")

step("gauges")
w.go("Dashboard")
pump(0.5)

step("features: read and switch auto headlights off")
fp = w.pages["Features"]
w.go("Features")
fp.select("sim-auto-lights-demo")
fp.read_status()
idle()
if fp.feature() and fp.feature().id == "sim-auto-lights-demo":
    fp.master_clicked(False)
    fp.apply_changes()
    idle(60)
else:
    problems.append("[features] couldn't select the auto headlights demo")

step("module settings: read, compare with factory, write, restore")
ab = w.pages["Configuration"]
w.go("Configuration")
idx = next((i for i in range(ab.mod.count()) if "726" in ab.mod.itemText(i)), 0)
ab.mod.setCurrentIndex(idx)
ab.read()
idle()
ab.compare_factory()
ab.write()
idle(60)
ab.restore_factory()
idle(60)
ab.backup_everything()
idle(60)

step("service jobs")
sp = w.pages["Service"]
w.go("Service")
for i, p in enumerate(sp.procs):
    if p.id == "sim-battery-reset":
        sp.list.setCurrentRow(i + 1)
        sp.run()
        idle(60)
for i, p in enumerate(sp.procs):
    if p.needs_values:
        sp.list.setCurrentRow(i + 1)
        before = len(urls)
        sp.run()
        if len(urls) == before:
            problems.append("[service] waiting-for-values job didn't open a request")
        break

step("trips and performance timer")
tp = w.pages["Trips"]
w.go("Trips")
tp.toggle_trip()
idle()
pump(2.0)
tp.stop_trip()
idle()
tp.toggle_perf()
pump(1.0)
tp.toggle_perf()
idle()

step("history, garage, library, community")
w.go("History")
w.pages["History"].select_vin(s.vin)
gp = w.pages["Garage"]
w.go("Garage")
gp.render()
if gp.cars or not gp.empty.isVisible():
    problems.append("[garage] the garage should start empty")
if not gp.add_card.isVisible():
    problems.append("[garage] no option to add the connected car")
gp.add_car()      # the owner adds the connected car
if len(gp.cars) != 1:
    problems.append("[garage] adding the connected car didn't work")
else:
    gp.tbl.selectRow(0)
    gp.rename()
    gp.open_history()
    gp.forget()
w.go("Library")
wl = A.WishListDialog(w, s)
if wl.items:
    wl.tbl.selectRow(0)
    wl.show_item(0)
    wl.claim()
w.go("Community")
w.pages["Community"].refresh()
idle()

step("settings: night mode on and off, problem report, update window")
st = w.pages["Settings"]
w.go("Settings")
st.night.setChecked(True)
pump(0.3)
st.night.setChecked(False)
pump(0.3)
pr = A.ProblemReportDialog(w)
pr.what.setPlainText("Test problem")
before = len(urls)
pr.send()
if len(urls) == before:
    problems.append("[problem] report didn't open GitHub")
elif s.vin and s.vin in urls[-1]:
    problems.append("[problem] VIN wasn't removed from the report")
w.update_rel = updates.Release("9.9.9", "https://github.com/x/y/releases/download/v9.9.9/H.exe", "notes", "a" * 64)
w.update_error = "test"
ud = A.UpdateDialog(w)
ud.done(0)
w.update_rel = None

step("disconnect, then an older car (OBD only)")
cp = w.pages["Connect"]
cp.toggle()
pump(0.3)
cp.port.setCurrentIndex(cp.port.findData(A.SIM_OLD))
cp.toggle()
wait(lambda: s.connected, 20, "connect to the older car")
idle()
mp.scan(False)
idle(60)
dp.read_all()
idle(60)
if dp.tbl.rowCount():
    dp.tbl.selectRow(0)
    dp.show_details()
    idle()
dp.mot_check()
idle()
dp.misfire_check()
idle()
for key in list(w.pages):
    w.go(key)
    pump(0.05)
cp.toggle()
pump(0.3)

step("a Mazda")
cp.port.setCurrentIndex(cp.port.findData(A.SIM_MAZDA))
cp.toggle()
wait(lambda: s.connected, 20, "connect to the Mazda")
idle()
mp.scan(False)
idle(60)
names = {m.name for m in s.modules.values()}
if "SAS - Airbags" not in names:
    problems.append(f"[mazda] Mazda module names not used: {sorted(names)}")
if s.any_car or w.nav.item(w.rows["Features"]).isHidden():
    problems.append("[mazda] Mazda was treated as 'any car'")
if mp.get_btn.isVisible():
    problems.append("[mazda] 'Get my factory file' shown for a Mazda")
dp.read_all()
idle(60)
w.go("Features")
pump(0.2)
cp.toggle()
pump(0.3)

step("any car mode (a Vauxhall)")
cp.port.setCurrentIndex(cp.port.findData(A.SIM_ANY))
cp.toggle()
wait(lambda: s.connected, 20, "connect to the Vauxhall")
idle()
if not s.any_car:
    problems.append("[any car] the Vauxhall didn't switch on any car mode")
if not all(w.nav.item(w.rows[k]).isHidden() for k in A.FORD_ONLY_PAGES):
    problems.append("[any car] Ford-only pages still in the sidebar")
mp.scan(False)
idle(60)
dp.read_all()
idle(60)
if "P0300" not in dp.table_codes():
    problems.append(f"[any car] engine code not read: {dp.table_codes()}")
before = opened.count("MisfireDialog")
dp.misfire_check()
idle()
dp.mot_check()
idle()
for key in list(w.pages):
    if not w.nav.item(w.rows[key]).isHidden():
        w.go(key)
        pump(0.05)
w.go("Home")
pump(0.1)
cp.toggle()
pump(0.3)

step("close")
w.close()
pump(0.3)

print()
if problems:
    print(f"{len(problems)} problem(s):")
    for p in problems:
        print("  " + p)
    sys.exit(1)
print(f"All screens OK. Dialogs opened: {', '.join(sorted(set(opened)))}")
sys.exit(0)
