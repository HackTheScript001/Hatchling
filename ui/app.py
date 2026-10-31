"""Hatchling - Ford HS/MS-CAN diagnostics desktop app (PySide6)."""
from __future__ import annotations

import html
import json
import math
import os
import sys
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from core import (asbuilt, dtc, factory, features, finder, history, library, livedata, modules, paths, report, services, updates,
                  vehicles, vin)
from core.version import VERSION
from core.buses import BUSES, OBD
from core.elm import DRIVER_PAGES, ELM327, HS, MS, ElmError, SerialTransport, describe_ports
from core.obd import GenericObd
from core.modules import Module
from core.pids import PIDS
from core.sim import SimTransport
from core.uds import UDS, UdsError
from ui import theme
from ui.theme import ACCENT, AMBER, CHANGED, DANGER, FAINT, INK, LINE, LINE_STRONG, MUTED, OK, PANEL_ALT, WARN

SIM = "Simulator - 2015 Fiesta (CAN)"
SIM_OLD = "Simulator - 2003 Focus (OBD only)"
AUTO = "Auto-detect from VIN"


# ---------------------------------------------------------------- background work
class Runner(QObject):
    """Runs adapter jobs one at a time off the UI thread, results back on the UI thread."""
    _done = Signal(object)
    progress = Signal(int, int)
    busy_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending = 0
        self._done.connect(self._finish)

    def run(self, fn: Callable, on_ok: Callable = None, on_err: Callable = None):
        self.pending += 1
        self.busy_changed.emit(True)

        def task():
            try:
                self._done.emit((on_ok, on_err, fn(), None))
            except Exception as e:  # noqa
                self._done.emit((on_ok, on_err, None, e))
        self.pool.submit(task)

    def _finish(self, pack):
        on_ok, on_err, result, err = pack
        self.pending -= 1
        if self.pending == 0:
            self.busy_changed.emit(False)
        if err is not None:
            if on_err:
                on_err(err)
            else:
                Session.instance.log(f"ERROR: {err}")
                QMessageBox.warning(None, "Hatchling", str(err))
        elif on_ok:
            on_ok(result)

    @property
    def busy(self):
        return self.pending > 0


class Session(QObject):
    instance: "Session" = None
    logged = Signal(str)
    connected_changed = Signal(bool)
    modules_changed = Signal()
    bus_changed = Signal(str)
    history_changed = Signal()
    features_changed = Signal()
    features_detected = Signal(object)
    factory_changed = Signal()
    codes_read = Signal(int)
    tech_changed = Signal(bool)   # {"states": {...}, "blocks": {...}} after a module scan

    def __init__(self):
        super().__init__()
        Session.instance = self
        self.runner = Runner()
        self.elm: Optional[ELM327] = None
        self.uds: Optional[UDS] = None
        self.simulated = False
        self.auto_switch = False
        self.modules: Dict[str, Module] = {}
        self.vin = ""
        self.vin_info: Optional[vin.VinInfo] = None
        self.profiles = vehicles.load_all()
        self.profile: Optional[vehicles.Profile] = None
        self.obd: Optional[GenericObd] = None
        self.factory: Optional[factory.FactoryData] = None
        self.voltage: Optional[float] = None
        self.tech = False          # show block/byte/bit details
        self.window: Optional[QWidget] = None
        self.open_config: Callable = lambda *a: None  # set by MainWindow
        self.focus_mode: Callable[[bool], None] = lambda on: None  # set by MainWindow
        try:
            self.history: Optional[history.History] = history.History()
        except Exception as e:  # history is a nice-to-have, never block the app
            print(f"History disabled: {e}")
            self.history = None

    def log(self, msg: str):
        self.logged.emit(time.strftime("%H:%M:%S  ") + msg)

    def record(self, kind: str, summary: str, data: Optional[dict] = None):
        """Adds an entry to this car's history."""
        if self.history is None:
            return
        try:
            self.history.add(self.vin or "unknown", kind, summary, data)
            self.history_changed.emit()
        except Exception as e:
            self.log(f"History not saved: {e}")

    @property
    def connected(self):
        return self.elm is not None

    def ensure_bus(self, bus: str, then: Callable[[], None]):
        """Get the adapter onto `bus`, asking the user to flip the switch if needed."""
        if not self.connected:
            return
        if self.elm.bus == bus:
            then()
            return
        new, cur = BUSES[bus], BUSES.get(self.elm.bus)
        if not (self.simulated or self.auto_switch) and (cur is None or cur.switch != new.switch):
            r = QMessageBox.information(
                self.window, "Switch bus",
                f"Set the switch on your adapter to <b>{new.switch}</b> "
                f"(for {new.label}, pins {new.pins}), then press OK.",
                QMessageBox.Ok | QMessageBox.Cancel)
            if r != QMessageBox.Ok:
                return

        def done(_):
            self.bus_changed.emit(bus)
            then()
        self.runner.run(lambda: self.elm.set_bus(bus), done)

    def per_bus(self, items: Dict[str, Callable[[], object]], on_each: Callable[[str, object], None],
                on_done: Callable[[], None] = lambda: None):
        """Run one job per bus in a sensible order, switching between them."""
        order = [b for b in BUSES if b in items]
        if self.elm and self.elm.bus in order:  # start on the bus we're already on
            order.sort(key=lambda b: b != self.elm.bus)

        def step(i):
            if i >= len(order):
                on_done()
                return
            bus = order[i]
            self.ensure_bus(bus, lambda: self.runner.run(
                items[bus], lambda r: (on_each(bus, r), step(i + 1))))
        step(0)

    @property
    def obd_only(self) -> bool:
        return bool(self.profile and self.profile.obd_only)

    def scan_buses(self) -> List[str]:
        return self.profile.can_buses if self.profile else [HS, MS]

    def module_for(self, bus: str, addr: int) -> Module:
        key = f"{bus}:{addr:03X}"
        if key not in self.modules:
            self.modules[key] = Module(addr, bus, modules.name_for(bus, addr))
        return self.modules[key]


# ---------------------------------------------------------------- helpers
def card() -> Tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    return f, lay


def page_header(title: str, subtitle: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 6)
    t = QLabel(title)
    t.setObjectName("title")
    s = QLabel(subtitle)
    s.setObjectName("subtle")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w


def button(text: str, kind: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setCursor(Qt.PointingHandCursor)
    return b


def table(headers: List[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def item(text, color: str = None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    if color:
        it.setForeground(QColor(color))
    return it


class Page(QWidget):
    def __init__(self, s: Session, title: str, subtitle: str):
        super().__init__()
        self.setObjectName("page")
        self.s = s
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(26, 22, 26, 18)
        self.root.setSpacing(12)
        self.root.addWidget(page_header(title, subtitle))

    def need_connection(self) -> bool:
        if not self.s.connected:
            QMessageBox.information(self, "Hatchling", "Connect to the adapter first.")
            return False
        return True


# ---------------------------------------------------------------- Home
class HomePage(Page):
    """Start screen: the car, how it's doing, and the everyday jobs in plain words."""

    def __init__(self, s):
        super().__init__(s, "", "")
        self.root.itemAt(0).widget().hide()          # no generic page header on Home
        self.go: Callable[[str], None] = lambda key: None   # set by MainWindow
        self.go_codes: Callable[[], None] = lambda: None
        self.carname = QLabel("No car connected")
        self.carname.setObjectName("carname")
        self.carline = QLabel("Plug the adapter into the port under the dashboard and switch the ignition on.")
        self.carline.setObjectName("subtle")
        self.carline.setWordWrap(True)
        self.root.addWidget(self.carname)
        self.root.addWidget(self.carline)
        self.connect_btn = button("Connect to the car", "primary")
        self.connect_btn.clicked.connect(lambda: self.go("Connect"))
        crow = QHBoxLayout()
        crow.addWidget(self.connect_btn)
        crow.addStretch()
        self.root.addLayout(crow)

        # what we know about the car right now
        self.figs = {}
        frow = QHBoxLayout()
        frow.setSpacing(28)
        for key, label in (("battery", "Battery"), ("modules", "Modules found"), ("codes", "Fault codes"),
                           ("features", "Features on")):
            box = QVBoxLayout()
            box.setSpacing(0)
            val = QLabel("-")
            val.setObjectName("figure")
            lab = QLabel(label)
            lab.setObjectName("subtle")
            box.addWidget(val)
            box.addWidget(lab)
            frow.addLayout(box)
            self.figs[key] = val
        frow.addStretch()
        self.fig_frame = QFrame()
        self.fig_frame.setObjectName("plain")
        fl = QVBoxLayout(self.fig_frame)
        fl.setContentsMargins(0, 14, 0, 4)
        fl.addLayout(frow)
        self.root.addWidget(self.fig_frame)

        # everyday jobs
        grid = QGridLayout()
        grid.setSpacing(10)
        jobs = (("Check for faults", "Read fault codes from every module, see what they mean, clear them.", "codes"),
                ("Turn features on or off", "Cruise control, lights and more - with a check that the parts are fitted.",
                 "Features"),
                ("Watch live data", "Engine speed, temperatures, battery and anything else you add.", "Live data"),
                ("Get more features", "Download features other owners have learned on the same car.", "Library"))
        for i, (title, text, key) in enumerate(jobs):
            b = QPushButton(f"{title}\n{text}")
            b.setObjectName("task")
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(70)
            b.clicked.connect(lambda _, k=key: self.job(k))
            grid.addWidget(b, i // 2, i % 2)
        self.root.addLayout(grid)

        # first-time steps (a real sequence, so numbered)
        self.steps_box = QFrame()
        self.steps_box.setObjectName("card")
        sl = QVBoxLayout(self.steps_box)
        sl.setContentsMargins(16, 12, 16, 12)
        head = QLabel("<b>Getting started</b>")
        sl.addWidget(head)
        self.step_labels = []
        for t in ("Plug the adapter in and switch the ignition on (engine can stay off)",
                  "Connect to the car",
                  "Import your car's factory file from Ford - so you can always undo changes",
                  "Scan the car to find its modules and see which features are on"):
            lab = QLabel(t)
            lab.setTextFormat(Qt.RichText)   # always formatted, even when a line has no tags
            lab.setWordWrap(True)
            sl.addWidget(lab)
            self.step_labels.append((lab, t))
        self.root.addWidget(self.steps_box)
        self.root.addStretch()

        self.counts = {"codes": None, "features": None}
        s.connected_changed.connect(lambda _: self.refresh())
        s.modules_changed.connect(self.refresh)
        s.factory_changed.connect(self.refresh)
        s.codes_read.connect(lambda n: (self.counts.__setitem__("codes", n), self.refresh()))
        s.features_detected.connect(self.on_features)
        self.refresh()

    def on_features(self, payload):
        on = sum(1 for parts in payload["states"].values()
                 if parts and all(v == features.ENABLED for v in parts.values()))
        self.counts["features"] = on
        self.refresh()

    def job(self, key: str):
        if key == "codes":
            self.go("Fault codes")
            if self.s.connected:
                self.go_codes()
            return
        self.go(key)

    def refresh(self):
        s = self.s
        if s.connected:
            info = s.vin_info
            if info:
                self.carname.setText(f"{info.make}, {info.year}" if info.year else info.make)
            else:
                self.carname.setText("Connected")
            bits = [f"VIN {s.vin}" if s.vin else "VIN not read"]
            if s.profile:
                bits.append(s.profile.name)
            self.carline.setText(". ".join(bits) + ("." if bits else ""))
            self.connect_btn.hide()
        else:
            self.carname.setText("No car connected")
            self.carline.setText("Plug the adapter into the port under the dashboard and switch the ignition on.")
            self.connect_btn.show()
        self.figs["battery"].setText(f"{s.voltage:.1f} V" if s.connected and s.voltage else "-")
        n_mods = len({m.addr for m in s.modules.values()})
        self.figs["modules"].setText(str(n_mods) if n_mods else "-")
        c = self.counts["codes"]
        self.figs["codes"].setText("-" if c is None else str(c))
        self.figs["codes"].setStyleSheet(f"color: {DANGER if c else INK};")
        f = self.counts["features"]
        self.figs["features"].setText("-" if f is None else str(f))
        done = [True, s.connected, bool(s.factory), bool(s.modules)]
        for (lab, t), d, n in zip(self.step_labels, done, range(1, 5)):
            lab.setText(f"<span style='color:{OK}'>✓</span>&nbsp; <s>{html.escape(t)}</s>" if d
                        else f"{n}.&nbsp; {html.escape(t)}")
            lab.setStyleSheet(f"color: {MUTED};" if d else "")
        self.steps_box.setVisible(not all(done))


# ---------------------------------------------------------------- Connect
class ConnectPage(Page):
    def __init__(self, s):
        super().__init__(s, "Connection", "Plug the adapter into the port under the dashboard and switch the ignition on - "
                                       "the engine can stay off. Pick the adapter below and connect.")
        c, lay = card()
        grid = QGridLayout()
        self.port = QComboBox()
        self.baud = QComboBox()
        self.baud.addItems(["38400", "115200", "57600", "500000"])
        self.vehicle = QComboBox()
        self.vehicle.setMinimumWidth(360)
        self.vehicle.addItem(AUTO, None)
        for p in s.profiles:
            self.vehicle.addItem(p.name, p.id)
        self.vehicle.currentIndexChanged.connect(self.vehicle_changed)
        refresh = button("Refresh")
        refresh.clicked.connect(self.refresh)
        grid.addWidget(QLabel("Port"), 0, 0)
        grid.addWidget(self.port, 0, 1)
        grid.addWidget(refresh, 0, 2)
        grid.addWidget(QLabel("Baud"), 1, 0)
        grid.addWidget(self.baud, 1, 1)
        grid.addWidget(QLabel("Vehicle"), 2, 0)
        grid.addWidget(self.vehicle, 2, 1, 1, 2)
        lay.addLayout(grid)
        self.models = QLabel("")
        self.models.setObjectName("subtle")
        self.models.setWordWrap(True)
        lay.addWidget(self.models)
        self.auto = QCheckBox("My adapter switches HS/MS-CAN automatically (no physical switch)")
        self.trace = QCheckBox("Show raw adapter traffic in the log")
        lay.addWidget(self.auto)
        lay.addWidget(self.trace)
        row = QHBoxLayout()
        self.btn = button("Connect", "primary")
        self.btn.clicked.connect(self.toggle)
        row.addWidget(self.btn)
        row.addStretch()
        lay.addLayout(row)
        self.root.addWidget(c)

        info, il = card()
        self.info = QLabel("Not connected")
        self.info.setObjectName("subtle")
        self.info.setWordWrap(True)
        il.addWidget(self.info)
        self.root.addWidget(info)
        self.root.addStretch()
        self.trace.toggled.connect(lambda v: setattr(s.elm, "trace", v) if s.elm else None)
        self.refresh()
        self.vehicle_changed()

    def refresh(self):
        self.port.clear()
        ports = describe_ports()
        for dev, label in ports:
            self.port.addItem(label, dev)
        self.port.addItem(SIM, SIM)
        self.port.addItem(SIM_OLD, SIM_OLD)
        if not ports and hasattr(self, "info") and not self.s.connected:
            self.info.setText(
                "No adapter found. Plug it in and press Refresh.<br><br>If it still doesn't appear, Windows "
                "needs its USB driver - usually it installs automatically within a minute. If not, get it "
                f"from the chip maker: <a href='{DRIVER_PAGES['FTDI']}' style='color:{ACCENT}'>FTDI</a> or "
                f"<a href='{DRIVER_PAGES['CH340']}' style='color:{ACCENT}'>CH340</a> (Device Manager shows "
                "which one under 'Other devices').")
            self.info.setOpenExternalLinks(True)

    def selected_profile(self) -> Optional[vehicles.Profile]:
        pid = self.vehicle.currentData()
        return next((p for p in self.s.profiles if p.id == pid), None)

    def vehicle_changed(self, *_):
        p = self.selected_profile()
        if p is None:
            self.models.setText("Hatchling reads the VIN after connecting and suggests the best match. "
                                "You can change it here at any time.")
        else:
            buses = ", ".join(BUSES[b].label for b in p.buses)
            self.models.setText(f"<b>Covers:</b> {p.models}<br><b>Buses:</b> {buses}<br>{p.notes}")
        if self.s.connected and p is not None and p is not self.s.profile:
            self.s.profile = p
            self.s.modules.clear()
            self.s.modules_changed.emit()
            self.s.log(f"Vehicle profile: {p.name}")
            self.s.connected_changed.emit(True)

    def toggle(self):
        if self.s.connected:
            self.s.elm.close()
            self.s.elm = self.s.uds = self.s.obd = None
            self.s.modules.clear()
            self.s.modules_changed.emit()
            self.s.connected_changed.emit(False)
            self.btn.setText("Connect")
            self.info.setText("Disconnected")
            self.s.log("Disconnected")
            return
        port = self.port.currentData() or self.port.currentText()
        sim = port in (SIM, SIM_OLD)
        legacy = port == SIM_OLD
        baud = int(self.baud.currentText())
        trace = self.trace.isChecked()
        chosen = self.selected_profile()
        self.btn.setEnabled(False)
        self.info.setText("Connecting...")

        def connect():
            t = SimTransport(legacy=legacy) if sim else SerialTransport(port, baud)
            elm = ELM327(t, log=self.s.log)
            elm.trace = trace
            try:
                elm.initialise()
                v = elm.voltage()
                uds, obd = UDS(elm), GenericObd(elm)
                # 1) CAN car? ask the PCM for its VIN on HS-CAN
                vin_txt, has_can, proto = "", False, ""
                if chosen is None or not chosen.obd_only:
                    try:
                        vin_txt = uds.read_did(0x7E0, 0xF190).decode("ascii", "ignore").strip("\x00 ")
                        has_can = True
                    except (UdsError, ElmError):
                        has_can = uds.tester_present(0x7E0) or uds.tester_present(0x760)
                # 2) otherwise generic OBD-II on whatever protocol the car uses
                if not has_can or not vin_txt:
                    elm.set_bus(OBD)
                    try:
                        proto = obd.connect()
                        vin_txt = vin_txt or (obd.vin() or "")
                    except ElmError:
                        pass
                    if has_can:
                        elm.set_bus(HS)
            except Exception:
                t.close()
                raise
            return elm, uds, obd, v, vin_txt, has_can, proto

        def ok(res):
            elm, uds, obd, v, vin_txt, has_can, proto = res
            s = self.s
            s.elm, s.uds, s.obd = elm, uds, obd
            s.simulated = sim
            s.auto_switch = self.auto.isChecked()
            s.vin = vin_txt
            s.vin_info = vin.decode(vin_txt)
            s.voltage = v
            s.factory = factory.load(vin_txt) if vin_txt else None
            s.factory_changed.emit()
            if chosen:
                s.profile = chosen
            else:
                s.profile = vehicles.suggest(s.profiles, s.vin_info, has_can)[0] if s.profiles else None
                idx = self.vehicle.findData(s.profile.id) if s.profile else 0
                self.vehicle.blockSignals(True)
                self.vehicle.setCurrentIndex(max(0, idx))
                self.vehicle.blockSignals(False)
                self.vehicle_changed()
            self.btn.setEnabled(True)
            self.btn.setText("Disconnect")
            vtxt = f"{v:.1f} V" if v else "unknown"
            car = s.vin_info.summary if s.vin_info else "VIN not read"
            year_hint = "" if (s.vin_info and s.vin_info.year) or not s.vin_info else \
                f"<br><span style='color:{WARN}'>This VIN doesn't carry the model year - check the vehicle " \
                "profile matches your car's generation.</span>"
            link = "CAN module access" if has_can else f"generic OBD-II only ({proto or 'no reply'})"
            self.info.setText(f"<b>{elm.version}</b> · battery {vtxt}<br>"
                              f"<b>{car}</b> · VIN {s.vin or '-'}<br>"
                              f"Link: {link}<br>Profile: <b>{s.profile.name if s.profile else '-'}</b>{year_hint}"
                              "<br><br>Next: go to <b>Modules</b> and run a scan.")
            s.log(f"Connected: {car}, profile {s.profile.name if s.profile else '-'}")
            s.record(history.CONNECT, f"{car} · {s.profile.name if s.profile else 'no profile'} · {vtxt}",
                     {"voltage": v, "profile": s.profile.id if s.profile else None, "link": link})
            s.connected_changed.emit(True)
            s.bus_changed.emit(elm.bus)

        def err(e):
            self.btn.setEnabled(True)
            self.info.setText(f"<span style='color:{DANGER}'>Could not connect: {e}</span><br>"
                              "Check the COM port, try another baud rate, and make sure the ignition is on.")
            self.s.log(f"Connect failed: {e}")
        self.s.runner.run(connect, ok, err)


# ---------------------------------------------------------------- Modules
class ModulesPage(Page):
    def __init__(self, s):
        super().__init__(s, "Modules", "The computers in your car - engine, brakes, dashboard and so on. Scanning finds "
                                       "them, reads their part numbers and checks which features are on.")
        row = QHBoxLayout()
        self.quick = button("Scan the car", "primary")
        self.full = button("Deep scan (slower)")
        self.quick.clicked.connect(lambda: self.scan(False))
        self.full.clicked.connect(lambda: self.scan(True))
        self.prog = QProgressBar()
        self.prog.setTextVisible(False)
        self.prog.setMaximumWidth(260)
        row.addWidget(self.quick)
        row.addWidget(self.full)
        row.addSpacing(12)
        row.addWidget(self.prog)
        row.addStretch()
        self.root.addLayout(row)
        frow = QHBoxLayout()
        self.factory_label = QLabel("")
        self.factory_label.setWordWrap(True)
        self.import_btn = button("Import factory file")
        self.import_btn.clicked.connect(lambda: import_factory_file(self, self.s))
        self.diff_btn = button("Changes since factory")
        self.diff_btn.clicked.connect(self.changes_since_factory)
        self.donor_btn = button("Compare with donor car")
        self.donor_btn.clicked.connect(self.compare_donor)
        frow.addWidget(self.factory_label, 1)
        frow.addWidget(self.import_btn)
        frow.addWidget(self.diff_btn)
        frow.addWidget(self.donor_btn)
        self.root.addLayout(frow)
        s.factory_changed.connect(self.show_factory)
        s.connected_changed.connect(lambda _: self.show_factory())
        self.tbl = table(["Address", "Bus", "Module", "Part number", "Software", "VIN"])
        self.root.addWidget(self.tbl, 1)
        self.feat_label = QLabel("")
        self.feat_label.setWordWrap(True)
        self.root.addWidget(self.feat_label)
        s.runner.progress.connect(lambda a, b: (self.prog.setMaximum(b), self.prog.setValue(a)))
        s.modules_changed.connect(self.render)

    def scan(self, full: bool):
        if not self.need_connection():
            return
        self.s.modules.clear()
        if self.s.obd_only:
            def job():
                return self.s.obd.connect()

            def done(proto):
                m = Module(0x7E0, OBD, f"Engine (OBD-II, {proto})", {"VIN": self.s.vin})
                self.s.modules[m.key] = m
                self.s.log(f"OBD-II engine computer found on {proto}")
                self.s.modules_changed.emit()
            self.s.ensure_bus(OBD, lambda: self.s.runner.run(job, done))
            return
        prog = self.s.runner.progress.emit
        feats = [f for f in features.load_all() if f.configured]
        needed = features.needed_modules(feats)
        collected: Dict[features.ModKey, asbuilt.Blocks] = {}
        present = set()

        def job(bus):
            def run():
                found = modules.scan(self.s.uds, bus, full, prog)
                # read the configuration of modules that features use while we're on this bus
                blocks = {}
                for m in found:
                    if (bus, m.addr) in needed:
                        try:
                            blocks[(bus, m.addr)] = asbuilt.read_blocks(self.s.uds, m)
                        except (UdsError, ElmError) as e:
                            self.s.log(f"{m.name}: couldn't read configuration for features - {e}")
                return found, blocks
            return run

        def each(bus, res):
            found, blocks = res
            for m in found:
                self.s.modules[m.key] = m
                present.add((m.bus, m.addr))
                if not self.s.vin and m.identity.get("VIN"):
                    self.s.vin = m.identity["VIN"]
            collected.update(blocks)
            self.s.log(f"{BUSES[bus].label}: {len(found)} module(s) found")
            self.s.modules_changed.emit()

        def done():
            mods = sorted(self.s.modules.values(), key=lambda m: (m.bus, m.addr))
            states = features.detect_all(feats, collected, present)
            groups = features.summary(feats, states)
            self.show_feature_summary(groups)
            self.s.features_detected.emit({"states": states, "blocks": collected})
            self.s.record(history.SCAN, f"{len(mods)} module(s) found"
                          + (f" · features on: {', '.join(groups[features.ENABLED])}"
                             if groups.get(features.ENABLED) else ""),
                          {"modules": [{"addr": f"{m.addr:03X}", "bus": m.bus, "name": m.name,
                                        "part": m.identity.get("Part number", ""),
                                        "software": m.identity.get("Software", "")} for m in mods],
                           "features": {k: v for k, v in groups.items()}})
        self.feat_label.setText("")
        self.s.per_bus({b: job(b) for b in self.s.scan_buses()}, each, done)

    def show_factory(self):
        f = self.s.factory
        if f:
            self.factory_label.setText(f"<span style='color:{OK}'>●</span> Factory As-Built loaded for "
                                       f"{f.vin} ({len(f.modules)} modules, imported {f.imported})")
            self.diff_btn.setEnabled(True)
            self.donor_btn.setEnabled(True)
        else:
            self.factory_label.setText(f"<span style='color:{WARN}'>●</span> No factory As-Built yet. Before "
                                       "changing anything, download it for your VIN (motorcraftservice.com, "
                                       "As-Built data) and import it here.")
            self.diff_btn.setEnabled(False)
            self.donor_btn.setEnabled(False)

    def compare_donor(self):
        """Your car's factory file vs a donor car's (same model, has the feature) - the differences
        include the feature's bits. No car reads needed; modules must have been scanned for their bus."""
        if not self.s.factory:
            return
        if not self.s.modules:
            QMessageBox.information(self, "Hatchling", "Run a module scan first so Hatchling knows which bus each "
                                                       "module is on and its part numbers.")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Ford As-Built file from a car that HAS the feature",
                                              os.path.expanduser("~"),
                                              "As-Built files (*.ab *.xml *.txt *.htm *.html);;All files (*.*)")
        if not path:
            return
        try:
            donor = factory.load_file(path)
        except (OSError, factory.FactoryError, ValueError) as e:
            QMessageBox.warning(self, "Hatchling", f"Couldn't read that file: {e}")
            return
        if donor.vin and donor.vin == self.s.factory.vin:
            QMessageBox.information(self, "Hatchling", "That's your own car's file - pick the donor car's file.")
            return
        diffs = factory.compare_cars(self.s.factory, donor)
        by_addr: Dict[int, Module] = {}
        for m in sorted(self.s.modules.values(), key=lambda m: m.bus != MS):
            by_addr.setdefault(m.addr, m)
        groups = [(by_addr[a], ch) for a, ch in diffs.items() if a in by_addr]
        skipped = [f"{a:03X}" for a in diffs if a not in by_addr]
        if not groups:
            QMessageBox.information(self, "Hatchling", "No differences in modules your car has." +
                                    (f" (Modules only on the donor: {', '.join(skipped)})" if skipped else ""))
            return
        d_info = vin.decode(donor.vin) if donor.vin else None
        mods = ", ".join(f"{m.name.split(' - ')[0]} ({len(ch)})" for m, ch in groups)
        FeatureFinderDialog(
            self, self.s, groups, title=f"Your car vs donor{' (' + d_info.summary + ')' if d_info else ''}: {mods}",
            before_label="Your factory", after_label="Donor", offer_flip=False,
            hint="These differ between the two cars as built. Only some are the feature - others are trim, "
                 "engine or option differences (lights, seats, region). Tick just the feature's bits; tip: a "
                 "donor as close to your car as possible (same year, engine, trim) gives the shortest list."
                 + (f" Modules only on the donor, not shown: {', '.join(skipped)}." if skipped else "")).exec()

    def changes_since_factory(self):
        """Reads every scanned module the factory file covers and lists what's changed since the factory."""
        if not self.need_connection() or not self.s.factory:
            return
        if not self.s.modules:
            QMessageBox.information(self, "Hatchling", "Run a module scan first.")
            return
        fac = self.s.factory
        cur_bus = self.s.elm.bus
        chosen: Dict[int, Module] = {}
        for m in sorted(self.s.modules.values(), key=lambda m: m.bus != cur_bus):
            if m.addr in fac.modules and m.addr not in chosen and m.bus in BUSES and BUSES[m.bus].protocol == "can":
                chosen[m.addr] = m
        if not chosen:
            QMessageBox.information(self, "Hatchling", "None of the scanned modules are in the factory file.")
            return
        jobs = {}
        for bus in {m.bus for m in chosen.values()}:
            mods = [m for m in chosen.values() if m.bus == bus]

            def job(mods=mods):
                out = []
                for m in mods:
                    try:
                        car = asbuilt.read_blocks(self.s.uds, m)
                    except (UdsError, ElmError) as e:
                        self.s.log(f"{m.name}: couldn't read - {e}")
                        continue
                    out.append((m, finder.compare(factory.align(fac.blocks(m.addr), car), car)))
                return out
            jobs[bus] = job
        groups = []

        def done():
            changed = [(m, ch) for m, ch in groups if ch]
            self.s.log(f"Changes since factory: {sum(len(ch) for _, ch in changed)} byte(s) in "
                       f"{len(changed)} of {len(groups)} module(s)")
            if not changed:
                QMessageBox.information(self, "Changes since factory",
                                        f"All {len(groups)} module(s) still match the factory settings.")
                return
            summary = ", ".join(f"{m.name.split(' - ')[0]} ({len(ch)})" for m, ch in changed)
            FeatureFinderDialog(
                self, self.s, changed, title=f"Changed since the factory: {summary}",
                before_label="Factory", after_label="Car now", offer_flip=False,
                hint="Everything that's different from how the car left the factory. Tick the changes that make up "
                     "one feature (e.g. the cruise control bits in the PCM and cluster), name it and save - your "
                     "car's current values become 'on' and the factory values 'off'. Run it again for the next "
                     "feature.").exec()
        self.s.per_bus(jobs, lambda b, r: groups.extend(r), done)

    def show_feature_summary(self, groups: Dict[str, List[str]]):
        if not groups:
            self.feat_label.setText("")
            return
        parts = []
        for st, label in ((features.ENABLED, "On"), (features.MIXED, "Partly on"), (features.DISABLED, "Off"),
                          (features.NOT_FOUND, "Module not on this car"), (features.UNKNOWN, "Couldn't read")):
            if groups.get(st):
                parts.append(f"<span style='color:{STATE_COLORS[st]}'>{label}:</span> {', '.join(groups[st])}")
        self.feat_label.setText("<b>Features detected</b><br>" + "<br>".join(parts))
        on = groups.get(features.ENABLED, [])
        self.s.log("Features detected: " + (", ".join(on) + " on" if on else "none on"))

    def render(self):
        self.tbl.setRowCount(0)
        for m in sorted(self.s.modules.values(), key=lambda m: (m.bus, m.addr)):
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            vals = [f"{m.addr:03X}", m.bus, m.name, m.identity.get("Part number", ""),
                    m.identity.get("Software", ""), m.identity.get("VIN", "")]
            for c, v in enumerate(vals):
                self.tbl.setItem(r, c, item(v, ACCENT if c == 0 else None))


# ---------------------------------------------------------------- DTCs
class DtcPage(Page):
    def __init__(self, s):
        super().__init__(s, "Fault codes", "Codes the car has stored when something went wrong. Double-click a code to "
                                           "see the conditions when it happened.")
        row = QHBoxLayout()
        self.read = button("Read fault codes", "primary")
        self.details = button("Details")
        self.clear = button("Clear fault codes", "danger")
        self.export = button("Save a report")
        self.read.clicked.connect(self.read_all)
        self.details.clicked.connect(self.show_details)
        self.clear.clicked.connect(self.clear_all)
        self.export.clicked.connect(self.export_report)
        for w in (self.read, self.details, self.clear):
            row.addWidget(w)
        row.addStretch()
        self.summary = QLabel("")
        self.summary.setObjectName("subtle")
        row.addWidget(self.summary)
        row.addWidget(self.export)
        self.root.addLayout(row)
        self.tbl = table(["Module", "Code", "Status", "Description"])
        self.tbl.itemDoubleClicked.connect(lambda _: self.show_details())
        self.root.addWidget(self.tbl, 1)
        self.found: List[Tuple[Module, bytes, int]] = []  # row -> (module, raw dtc, status)

    def _by_bus(self):
        out: Dict[str, List[Module]] = {}
        seen = set()
        for m in sorted(self.s.modules.values(), key=lambda m: (m.bus, m.addr)):
            if m.addr in seen:  # BCM visible on both buses - read once
                continue
            seen.add(m.addr)
            out.setdefault(m.bus, []).append(m)
        return out

    def read_all(self):
        if not self.need_connection():
            return
        if not self.s.modules:
            QMessageBox.information(self, "Hatchling", "Scan for modules first.")
            return
        self.tbl.setRowCount(0)
        self.found = []
        if self.s.obd_only:
            self.read_obd()
            return

        def job(mods):
            def run():
                res = []
                for m in mods:
                    try:
                        res.append((m, self.s.uds.read_dtcs(m.addr)))
                    except UdsError as e:
                        res.append((m, e))
                return res
            return run

        def each(bus, res):
            for m, codes in res:
                if isinstance(codes, Exception):
                    self.add(m.name, "-", f"Could not read: {codes}", "", WARN)
                    self.found.append((m, b"", 0))
                    continue
                for raw, st in codes:
                    code = dtc.decode(raw)
                    color = DANGER if st & 0x09 else WARN
                    self.add(m.name, code, dtc.describe_status(st), dtc.description(code), color)
                    self.found.append((m, raw, st))

        def done():
            n = sum(1 for _, raw, _ in self.found if raw)
            self.summary.setText(f"{n} code(s)" if n else "No fault codes")
            self.s.log(f"DTC read complete: {n} code(s)")
            self.record_codes()
        self.s.per_bus({b: job(ms) for b, ms in self._by_bus().items()}, each, done)

    def read_obd(self):
        """Generic OBD-II codes from the engine computer (pre-CAN cars)."""
        m = next(iter(self.s.modules.values()))

        def job():
            return self.s.obd.stored_dtcs(), self.s.obd.pending_dtcs()

        def ok(res):
            stored, pending = res
            for code in stored:
                self.add("Engine", code, "Confirmed", dtc.description(code), DANGER)
                self.found.append((m, b"", 0x08))
            for code in pending:
                if code not in stored:
                    self.add("Engine", code, "Pending", dtc.description(code), WARN)
                    self.found.append((m, b"", 0x04))
            n = len(self.found)
            self.summary.setText(f"{n} code(s)" if n else "No fault codes")
            self.s.log(f"OBD-II codes: {n}")
            self.record_codes()
        self.s.ensure_bus(OBD, lambda: self.s.runner.run(job, ok))

    def table_codes(self) -> List[str]:
        return [self.tbl.item(r, 1).text() for r in range(self.tbl.rowCount()) if self.tbl.item(r, 1).text() != "-"]

    def record_codes(self):
        codes = self.table_codes()
        self.s.codes_read.emit(len(codes))
        detail = [{"module": self.tbl.item(r, 0).text(), "code": self.tbl.item(r, 1).text(),
                   "status": self.tbl.item(r, 2).text()} for r in range(self.tbl.rowCount())
                  if self.tbl.item(r, 1).text() != "-"]
        self.s.record(history.CODES, f"{len(codes)} code(s)" + (f": {', '.join(codes[:6])}" if codes else ""),
                      {"codes": codes, "detail": detail})
        if self.s.history and self.s.vin:
            ch = self.s.history.code_changes(self.s.vin)
            if ch and (ch["new"] or ch["gone"]):
                parts = []
                if ch["new"]:
                    parts.append(f"new: {', '.join(ch['new'])}")
                if ch["gone"]:
                    parts.append(f"gone: {', '.join(ch['gone'])}")
                self.summary.setText(self.summary.text() + f"  ·  since {ch['since']} - " + "; ".join(parts))

    def add(self, mod, code, status, desc, color):
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        for c, v in enumerate([mod, code, status, desc]):
            self.tbl.setItem(r, c, item(v, color if c == 1 else None))

    def show_details(self):
        r = self.tbl.currentRow()
        if r < 0 or r >= len(self.found) or not self.found[r][1] or not self.need_connection():
            return
        m, raw, st = self.found[r]
        code = dtc.decode(raw)

        def job():
            out = {}
            for name, fn in (("Freeze frame", self.s.uds.dtc_snapshot), ("Extended data", self.s.uds.dtc_extended)):
                try:
                    out[name] = fn(m.addr, raw)
                except UdsError as e:
                    out[name] = str(e)
            return out

        def ok(res):
            parts = [f"<h3>{code} - {m.name}</h3><p>{dtc.description(code) or ''}</p>"
                     f"<p><b>Status:</b> {dtc.describe_status(st)} (0x{st:02X})</p>"]
            for name, v in res.items():
                if isinstance(v, (bytes, bytearray)):
                    body = decode_snapshot(v) if name == "Freeze frame" else v.hex(" ").upper() or "none"
                else:
                    body = f"<span style='color:{MUTED}'>{v}</span>"
                parts.append(f"<p><b>{name}</b><br>{body}</p>")
            box = QMessageBox(self)
            box.setWindowTitle(code)
            box.setText("".join(parts))
            box.exec()
        self.s.ensure_bus(m.bus, lambda: self.s.runner.run(job, ok))

    def clear_all(self):
        if not self.need_connection() or not self.s.modules:
            return
        if QMessageBox.question(self, "Clear codes", "Clear fault codes in every module?") != QMessageBox.Yes:
            return
        self.s.record(history.CLEAR, f"Cleared {len(self.table_codes())} code(s)", {"codes": self.table_codes()})
        if self.s.obd_only:
            self.s.ensure_bus(OBD, lambda: self.s.runner.run(self.s.obd.clear_dtcs, lambda _: self.read_all()))
            return

        def job(mods):
            def run():
                for m in mods:
                    try:
                        self.s.uds.clear_dtcs(m.addr)
                    except UdsError as e:
                        self.s.log(f"{m.name}: clear failed - {e}")
            return run
        self.s.per_bus({b: job(ms) for b, ms in self._by_bus().items()}, lambda b, r: None, self.read_all)

    def export_report(self):
        if not self.s.modules:
            QMessageBox.information(self, "Hatchling", "Scan for modules first (and read codes) to build a report.")
            return
        rows = [tuple(self.tbl.item(r, c).text() for c in range(4)) for r in range(self.tbl.rowCount())
                if self.tbl.item(r, 1).text() != "-"]
        volts = None
        if self.s.connected:
            try:
                volts = self.s.elm.voltage()
            except Exception:
                pass
        mods = {m.addr: m for m in self.s.modules.values()}.values()
        path = report.write_html(self.s.vin, volts, list(mods), rows)
        self.s.log(f"Report saved: {path}")
        import webbrowser
        webbrowser.open("file:///" + path.replace(os.sep, "/"))


def decode_snapshot(data: bytes) -> str:
    """Splits a UDS snapshot (record, count, then DID + value pairs) into readable lines.
    Value lengths aren't in the message, so known 2-byte DIDs are assumed and the rest shown raw."""
    if len(data) < 2:
        return data.hex(" ").upper() or "none"
    rec, count, rest = data[0], data[1], data[2:]
    lines = [f"Record {rec:02X}, {count} value(s)"]
    for _ in range(count):
        if len(rest) < 3:
            break
        did = (rest[0] << 8) | rest[1]
        size = 2 if did in (0xDD01,) else 1
        val, rest = rest[2:2 + size], rest[2 + size:]
        lines.append(f"&nbsp;&nbsp;{did:04X} = {val.hex(' ').upper()}")
    if rest:
        lines.append(f"&nbsp;&nbsp;remaining: {rest.hex(' ').upper()}")
    return "<br>".join(lines)


# ---------------------------------------------------------------- Live data
class Spark(QWidget):
    def __init__(self, lo, hi):
        super().__init__()
        self.lo, self.hi = lo, hi
        self.vals = deque(maxlen=120)
        self.setMinimumHeight(28)

    def push(self, v):
        self.vals.append(v)
        self.update()

    def paintEvent(self, _):
        if len(self.vals) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(ACCENT), 1.6))
        w, h = self.width(), self.height() - 4
        span = (self.hi - self.lo) or 1
        n = self.vals.maxlen
        pts = []
        for i, v in enumerate(self.vals):
            x = w - (len(self.vals) - 1 - i) * w / (n - 1)
            y = 2 + h - max(0.0, min(1.0, (v - self.lo) / span)) * h
            pts.append((x, y))
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            p.drawLine(int(x1), int(y1), int(x2), int(y2))


class LivePage(Page):
    def __init__(self, s):
        super().__init__(s, "Live data", "Readings from the car as it runs. Tick what you want to watch - fewer ticks "
                                         "update faster.")
        row = QHBoxLayout()
        self.btn = button("Start", "primary")
        self.btn.clicked.connect(self.toggle)
        self.rec_btn = button("Record to CSV")
        self.rec_btn.clicked.connect(self.toggle_record)
        self.bus_box = QComboBox()
        self.bus_box.currentIndexChanged.connect(self.change_bus)
        s.connected_changed.connect(lambda _: self.fill_buses())
        reload_btn = button("Reload list")
        reload_btn.clicked.connect(self.build)
        row.addWidget(self.btn)
        row.addWidget(self.rec_btn)
        row.addSpacing(12)
        row.addWidget(QLabel("Bus"))
        row.addWidget(self.bus_box)
        row.addStretch()
        self.rec_label = QLabel("")
        self.rec_label.setObjectName("subtle")
        row.addWidget(self.rec_label)
        row.addWidget(reload_btn)
        self.root.addLayout(row)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["", "Value", "Name", "Source", "Trend"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.tbl.horizontalHeader()
        for c in range(4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.root.addWidget(self.tbl, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.poll)
        self.polling = False
        self.recorder: Optional[livedata.Recorder] = None
        s.bus_changed.connect(self.on_bus)
        self.build()

    @property
    def bus(self) -> str:
        return self.bus_box.currentData() or HS

    def fill_buses(self):
        self.bus_box.blockSignals(True)
        self.bus_box.clear()
        for b in (self.s.profile.buses if self.s.profile else [HS, MS]):
            self.bus_box.addItem(BUSES[b].label, b)
        self.bus_box.blockSignals(False)
        self.on_bus(self.s.elm.bus if self.s.elm else self.bus)

    def available(self, sig, bus: str) -> bool:
        if sig.kind == "obd":
            return bus in (HS, OBD)
        return sig.bus == bus

    def build(self):
        self.signals = livedata.load_all()
        self.checks, self.sparks = [], []
        self.tbl.setRowCount(len(self.signals))
        for r, sig in enumerate(self.signals):
            cb = QCheckBox()
            cb.setChecked(sig.kind == "obd" and r < 5)
            self.checks.append(cb)
            self.tbl.setCellWidget(r, 0, cb)
            self.tbl.setItem(r, 1, item("-", ACCENT))
            self.tbl.setItem(r, 2, item(f"{sig.name} ({sig.unit})" if sig.unit else sig.name))
            self.tbl.setItem(r, 3, item(sig.group))
            sp = Spark(sig.lo, sig.hi)
            self.sparks.append(sp)
            self.tbl.setCellWidget(r, 4, sp)
            self.tbl.setRowHeight(r, 34)
        self.fill_buses()

    def on_bus(self, bus: str):
        self.bus_box.blockSignals(True)
        idx = self.bus_box.findData(bus)
        if idx >= 0:
            self.bus_box.setCurrentIndex(idx)
        self.bus_box.blockSignals(False)
        for r, sig in enumerate(self.signals):
            on_bus = self.available(sig, bus)
            self.checks[r].setEnabled(on_bus)
            for c in (1, 2, 3):
                it = self.tbl.item(r, c)
                it.setForeground(QColor(ACCENT if c == 1 and on_bus else INK if on_bus else FAINT))

    def change_bus(self, _):
        if self.s.connected:
            was = self.timer.isActive()
            self.timer.stop()
            self.s.ensure_bus(self.bus, lambda: self.timer.start() if was else None)
        else:
            self.on_bus(self.bus)

    def toggle(self):
        if self.timer.isActive():
            self.stop()
            return
        if not self.need_connection():
            return
        self.s.ensure_bus(self.bus, self._start)

    def _start(self):
        self.timer.start()
        self.btn.setText("Stop")

    def stop(self):
        self.timer.stop()
        self.btn.setText("Start")
        if self.recorder:
            self.toggle_record()

    def selected(self) -> List[int]:
        return [r for r, cb in enumerate(self.checks) if cb.isChecked() and cb.isEnabled()]

    def toggle_record(self):
        if self.recorder:
            self.recorder.close()
            self.s.log(f"Recorded {self.recorder.rows} rows to {self.recorder.path}")
            self.rec_label.setText(f"Saved {os.path.basename(self.recorder.path)}")
            self.recorder = None
            self.rec_btn.setText("Record to CSV")
            return
        rows = self.selected()
        if not rows:
            QMessageBox.information(self, "Hatchling", "Tick at least one value to record.")
            return
        self.recorder = livedata.Recorder([self.signals[r] for r in rows])
        self.rec_rows = rows
        self.rec_btn.setText("Stop recording")
        self.rec_label.setText("● Recording")
        if not self.timer.isActive():
            self.toggle()

    def poll(self):
        if self.polling or self.s.runner.busy or not self.s.connected:
            return
        rows = self.selected()
        if not rows:
            return
        self.polling = True

        def job():
            out = {}
            for r in rows:
                try:
                    sig = self.signals[r]
                    if sig.kind == "obd" and self.s.elm.bus == OBD:
                        out[r] = sig.formula(self.s.obd.pid(sig.ident))
                    else:
                        out[r] = livedata.read(self.s.uds, sig)
                except (UdsError, ElmError, IndexError, ZeroDivisionError, ValueError):
                    out[r] = None
            return out

        def ok(vals):
            self.polling = False
            for r, v in vals.items():
                if v is None:
                    self.tbl.item(r, 1).setText("n/a")
                    continue
                self.tbl.item(r, 1).setText(f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}")
                self.sparks[r].push(v)
            if self.recorder:
                self.recorder.add({self.signals[r].key: v for r, v in vals.items()})
                self.rec_label.setText(f"● Recording - {self.recorder.rows} rows")

        def err(e):
            self.polling = False
            self.stop()
            self.s.log(f"Live data stopped: {e}")
        self.s.runner.run(job, ok, err)

    def hideEvent(self, e):
        if self.timer.isActive() and not self.recorder:
            self.stop()
        super().hideEvent(e)


# ---------------------------------------------------------------- Feature finder
class FeatureFinderDialog(QDialog):
    """Shows bit-level differences (one or several modules) and saves them as a feature.

    groups: [(module, [ByteChange])] where 'before' is treated as off and 'after' as on,
    unless the user ticks that their car already has the feature (then it's flipped)."""

    def __init__(self, parent, s: Session, groups, title: str = "", before_label: str = "Before",
                 after_label: str = "After", offer_flip: bool = True, hint: str = ""):
        super().__init__(parent)
        self.s = s
        self.rows = [(m, c) for m, changes in groups for c in changes]
        self.setWindowTitle("Feature finder")
        self.setMinimumSize(680, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(8)
        n_mod = len(groups)
        t = QLabel(title or f"{len(self.rows)} byte(s), {sum(len(c.bits) for _, c in self.rows)} bit(s) differ"
                            f" across {n_mod} module(s)")
        t.setStyleSheet("font-size: 12pt; font-weight: 600;")
        t.setWordWrap(True)
        lay.addWidget(t)
        h = QLabel(hint or "Untick anything that isn't part of the feature (e.g. serial or mileage bits). "
                           f"{before_label} = off, {after_label.lower()} = on.")
        h.setObjectName("subtle")
        h.setWordWrap(True)
        lay.addWidget(h)
        self.tbl = QTableWidget(len(self.rows), 6)
        self.tbl.setHorizontalHeaderLabels(["Use", "Module", "Block", "Byte", f"{before_label} → {after_label.lower()}",
                                            "Bits changed"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.checks = []
        for r, (m, c) in enumerate(self.rows):
            cb = QCheckBox()
            cb.setChecked(True)
            self.checks.append(cb)
            self.tbl.setCellWidget(r, 0, cb)
            self.tbl.setItem(r, 1, item(f"{m.name} ({m.addr:03X})"))
            self.tbl.setItem(r, 2, item(f"{c.did:04X}", ACCENT))
            self.tbl.setItem(r, 3, item(c.byte))
            self.tbl.setItem(r, 4, item(f"{c.before:02X} → {c.after:02X}"))
            self.tbl.setItem(r, 5, item(", ".join(f"bit {b}: {c.before >> b & 1}→{c.after >> b & 1}"
                                                  for b in c.bits)))
        lay.addWidget(self.tbl, 1)
        self.flip = QCheckBox(f"My car already has this feature - its current values are 'on'")
        self.flip.setVisible(offer_flip)
        lay.addWidget(self.flip)
        form = QGridLayout()
        self.target = QComboBox()
        self.target.addItem("New feature", None)
        for f in features.load_all():
            if f.path.startswith(paths.user_dir("features")):
                self.target.addItem(f"Add to: {f.name}", f.path)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Cruise control")
        self.desc = QLineEdit()
        self.desc.setPlaceholderText("What turning it on does")
        self.hw = QLineEdit()
        self.hw.setPlaceholderText("Parts that must be fitted first")
        for r, (lbl, w) in enumerate((("Save to", self.target), ("Name", self.name),
                                      ("Description", self.desc), ("Hardware needed", self.hw))):
            form.addWidget(QLabel(lbl), r, 0)
            form.addWidget(w, r, 1)
        lay.addLayout(form)
        self.target.currentIndexChanged.connect(
            lambda _: [w.setEnabled(self.target.currentData() is None) for w in (self.name, self.desc, self.hw)])
        row = QHBoxLayout()
        row.addStretch()
        cancel, save = button("Cancel"), button("Save feature", "primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.save)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)

    def identity(self, m: Module) -> dict:
        """Part/software numbers of the module, from the scan if this dialog's copy doesn't have them."""
        if m.identity.get("Part number"):
            return m.identity
        scanned = self.s.modules.get(m.key)
        return scanned.identity if scanned else {}

    def chosen_groups(self):
        out: Dict[str, Tuple[Module, List[finder.ByteChange]]] = {}
        for (m, c), cb in zip(self.rows, self.checks):
            if not cb.isChecked():
                continue
            if self.flip.isChecked():
                c = finder.ByteChange(c.did, c.byte, c.after, c.before)
            out.setdefault(m.key, (m, []))[1].append(c)
        return list(out.values())

    def save(self):
        groups = self.chosen_groups()
        if not groups:
            QMessageBox.warning(self, "Hatchling", "Tick at least one change.")
            return
        path = self.target.currentData()
        if not path:
            name = self.name.text().strip()
            if not name:
                QMessageBox.warning(self, "Hatchling", "Give the feature a name.")
                return
            m, ch = groups[0]
            car = self.s.vin_info.summary if self.s.vin_info else ""
            feat = finder.make_feature(name, m.bus, m.addr, m.name, ch, self.desc.text().strip(), "Custom",
                                       self.hw.text().strip(), car, identity=self.identity(m), learned_on=car)
            path = finder.save_feature(feat)
            groups = groups[1:]
        for m, ch in groups:
            finder.merge_into(path, m.bus, m.addr, m.name, ch, self.identity(m))
        self.s.log(f"Feature saved: {path}")
        self.s.features_changed.emit()
        QMessageBox.information(self, "Feature saved",
                                f"Saved to {path}<br><br>It's marked <b>not verified</b>. Once you've checked it "
                                "works, set \"verified\": true in the file.<br><br>Run a module scan and the "
                                "Features page will show whether it's on.")
        self.accept()


def import_factory_file(parent, s: "Session") -> bool:
    """Asks for Ford's As-Built file for this car, checks the VIN and stores it."""
    path, _ = QFileDialog.getOpenFileName(parent, "Ford As-Built file for this car", os.path.expanduser("~"),
                                          "As-Built files (*.ab *.xml *.txt *.htm *.html);;All files (*.*)")
    if not path:
        return False
    try:
        data = factory.load_file(path)
    except (OSError, factory.FactoryError, ValueError) as e:
        QMessageBox.warning(parent, "Hatchling", f"Couldn't read that file: {e}")
        return False
    if not data.vin:
        data.vin = s.vin
    if s.vin and data.vin and data.vin != s.vin:
        if QMessageBox.warning(parent, "Different car",
                               f"This As-Built file is for VIN <b>{data.vin}</b>, but the connected car is "
                               f"<b>{s.vin}</b>.<br><br>Comparing them will show differences between two cars, "
                               "not changes to yours. Import anyway?",
                               QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return False
    if not data.vin:
        QMessageBox.information(parent, "Hatchling", "The file doesn't contain a VIN. Connect to the car first so "
                                                     "Hatchling knows which car it belongs to, then import again.")
        return False
    saved = factory.save(data)
    if data.vin == s.vin:
        s.factory = data
    mods = ", ".join(f"{a:03X}" for a in data.modules)
    s.log(f"Factory As-Built imported for {data.vin}: {len(data.lines)} lines, modules {mods}")
    s.record(history.NOTE, f"Imported factory As-Built ({len(data.lines)} lines, {len(data.modules)} modules)",
             {"file": data.source, "modules": mods, "saved": saved})
    s.factory_changed.emit()
    QMessageBox.information(parent, "Factory As-Built imported",
                            f"<b>{len(data.lines)}</b> lines for <b>{len(data.modules)}</b> modules ({mods}).<br><br>"
                            "Next: run a module scan, then <b>Changes since factory</b> on the Modules page shows "
                            "everything that's different from how the car left the factory.")
    return True


# ---------------------------------------------------------------- As-Built
class AsBuiltPage(Page):
    MAXCOLS = 12

    def __init__(self, s):
        super().__init__(s, "Module settings", "The raw settings stored in each module. For experienced users - "
                                                    "everyday changes are easier on the Features page. Every change is "
                                                    "backed up first and checked after writing.")
        row = QHBoxLayout()
        self.mod = QComboBox()
        self.mod.setMinimumWidth(280)
        self.read_btn = button("Read", "primary")
        self.save_btn = button("Save backup")
        self.load_btn = button("Load backup")
        self.import_btn = button("Import As-Built")
        self.finder_btn = button("Make feature from edits")
        self.compare_btn = button("Compare backups")
        self.factory_btn = button("Compare with factory")
        self.revert_btn = button("Undo edits")
        self.write_btn = button("Write changes", "danger")
        for b in (self.read_btn, self.save_btn, self.load_btn, self.revert_btn, self.write_btn):
            b.setEnabled(b is self.read_btn or b is self.load_btn)
        self.read_btn.clicked.connect(self.read)
        self.save_btn.clicked.connect(self.save)
        self.load_btn.clicked.connect(self.load)
        self.import_btn.clicked.connect(self.import_text)
        self.finder_btn.clicked.connect(self.make_feature)
        self.compare_btn.clicked.connect(self.compare_backups)
        self.factory_btn.clicked.connect(self.compare_factory)
        self.revert_btn.clicked.connect(lambda: self.show_blocks(self.original))
        self.write_btn.clicked.connect(self.write)
        row.addWidget(self.mod)
        for b in (self.read_btn, self.save_btn, self.load_btn, self.import_btn, self.revert_btn):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.write_btn)
        self.root.addLayout(row)
        frow = QHBoxLayout()
        flabel = QLabel("Feature finder")
        flabel.setObjectName("subtle")
        frow.addWidget(flabel)
        frow.addWidget(self.finder_btn)
        frow.addWidget(self.compare_btn)
        frow.addWidget(self.factory_btn)
        frow.addStretch()
        self.root.addLayout(frow)

        self.tbl = QTableWidget(0, self.MAXCOLS + 1)
        self.tbl.setHorizontalHeaderLabels(["Block"] + [f"B{i}" for i in range(self.MAXCOLS)])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tbl.itemChanged.connect(self.cell_changed)
        self.tbl.currentCellChanged.connect(lambda r, c, *_: self.select_byte(r, c))
        self.root.addWidget(self.tbl, 1)

        bits, bl = card()
        top = QHBoxLayout()
        self.bit_label = QLabel("Select a byte to edit its bits")
        self.bit_label.setObjectName("subtle")
        top.addWidget(self.bit_label)
        top.addStretch()
        bl.addLayout(top)
        brow = QHBoxLayout()
        self.bit_boxes = []
        for i in range(7, -1, -1):
            cb = QCheckBox(f"bit {i}")
            cb.setEnabled(False)
            cb.toggled.connect(self.bits_changed)
            self.bit_boxes.insert(0, cb)
            brow.addWidget(cb)
        brow.addStretch()
        bl.addLayout(brow)
        self.root.addWidget(bits)

        self.module: Optional[Module] = None
        self.original: asbuilt.Blocks = {}
        self.dids: List[int] = []
        self._loading = False
        self._sel = None
        s.modules_changed.connect(self.fill_modules)

    def fill_modules(self):
        self.mod.clear()
        seen = set()
        for m in sorted(self.s.modules.values(), key=lambda m: (m.bus, m.addr)):
            if m.addr in seen and m.bus == MS:  # BCM on both buses - keep one
                continue
            seen.add(m.addr)
            self.mod.addItem(m.label, m.key)

    def current_module(self) -> Optional[Module]:
        return self.s.modules.get(self.mod.currentData())

    def read(self):
        self._read()

    def _read(self, then: Optional[Callable[[], None]] = None):
        if not self.need_connection():
            return
        m = self.current_module()
        if not m:
            QMessageBox.information(self, "Hatchling", "Scan for modules first.")
            return
        prog = self.s.runner.progress.emit

        def ok(blocks):
            if not blocks:
                QMessageBox.information(self, "Hatchling", f"{m.name} returned no configuration blocks.")
                return
            self.module, self.original = m, blocks
            self.show_blocks(blocks)
            for b in (self.save_btn, self.revert_btn, self.write_btn):
                b.setEnabled(True)
            self.s.log(f"Read {len(blocks)} block(s) from {m.name}")
            if then:
                then()
        self.s.ensure_bus(m.bus, lambda: self.s.runner.run(lambda: asbuilt.read_blocks(self.s.uds, m, prog), ok))

    def open_module(self, bus: str, addr: int, did: Optional[int] = None, byte: Optional[int] = None):
        """Jump here from Features: select the module, read it and highlight the block/byte."""
        m = self.s.module_for(bus, addr)
        idx = self.mod.findData(m.key)
        if idx < 0:
            self.mod.addItem(m.label, m.key)
            idx = self.mod.count() - 1
        self.mod.setCurrentIndex(idx)

        def highlight():
            if did in self.dids:
                r = self.dids.index(did)
                c = (byte or 0) + 1
                self.tbl.setCurrentCell(r, c)
                self.tbl.scrollToItem(self.tbl.item(r, c))
                self.select_byte(r, c)
        self._read(highlight)

    def show_blocks(self, blocks: asbuilt.Blocks):
        self._loading = True
        self.dids = sorted(blocks)
        self.tbl.setRowCount(len(self.dids))
        for r, did in enumerate(self.dids):
            hdr = item(asbuilt.did_name(did), ACCENT)
            hdr.setFlags(Qt.ItemIsEnabled)
            self.tbl.setItem(r, 0, hdr)
            data = blocks[did]
            for c in range(self.MAXCOLS):
                if c < len(data):
                    it = QTableWidgetItem(f"{data[c]:02X}")
                    it.setTextAlignment(Qt.AlignCenter)
                else:
                    it = QTableWidgetItem("")
                    it.setFlags(Qt.NoItemFlags)
                self.tbl.setItem(r, c + 1, it)
        self._loading = False
        self.recolor()

    def edited_blocks(self) -> asbuilt.Blocks:
        out = {}
        for r, did in enumerate(self.dids):
            n = len(self.original.get(did, b""))
            vals = []
            for c in range(n):
                try:
                    vals.append(int(self.tbl.item(r, c + 1).text(), 16) & 0xFF)
                except ValueError:
                    vals.append(self.original[did][c])
            out[did] = bytes(vals)
        return out

    def recolor(self):
        self._loading = True
        for r, did in enumerate(self.dids):
            orig = self.original.get(did, b"")
            for c in range(len(orig)):
                it = self.tbl.item(r, c + 1)
                try:
                    changed = int(it.text(), 16) != orig[c]
                except ValueError:
                    changed = True
                it.setBackground(QColor(CHANGED) if changed else QColor(0, 0, 0, 0))
        self._loading = False

    def cell_changed(self, it: QTableWidgetItem):
        if self._loading or it.column() == 0:
            return
        txt = it.text().strip().upper()
        try:
            v = int(txt, 16)
            if not 0 <= v <= 255:
                raise ValueError
            self._loading = True
            it.setText(f"{v:02X}")
            self._loading = False
        except ValueError:
            did = self.dids[it.row()]
            self._loading = True
            it.setText(f"{self.original[did][it.column() - 1]:02X}")
            self._loading = False
        self.recolor()
        self.select_byte(it.row(), it.column())

    def select_byte(self, r, c):
        valid = 0 <= r < len(self.dids) and 1 <= c <= len(self.original.get(self.dids[r], b""))
        self._sel = (r, c) if valid else None
        for cb in self.bit_boxes:
            cb.setEnabled(valid)
        if not valid:
            self.bit_label.setText("Select a byte to edit its bits")
            return
        v = int(self.tbl.item(r, c).text(), 16)
        self.bit_label.setText(f"Block {asbuilt.did_name(self.dids[r])}, byte {c - 1} = 0x{v:02X}")
        self._loading = True
        for i, cb in enumerate(self.bit_boxes):
            cb.setChecked(bool(v >> i & 1))
        self._loading = False

    def bits_changed(self):
        if self._loading or not self._sel:
            return
        r, c = self._sel
        v = sum(1 << i for i, cb in enumerate(self.bit_boxes) if cb.isChecked())
        self.tbl.item(r, c).setText(f"{v:02X}")  # triggers cell_changed

    def save(self):
        if not self.module:
            return
        path = asbuilt.save_backup(self.module, self.edited_blocks(), self.s.vin, "manual backup")
        self.s.log(f"Backup saved: {path}")
        QMessageBox.information(self, "Backup saved", path)

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load backup", asbuilt.BACKUP_DIR, "Hatchling backup (*.json)")
        if not path:
            return
        try:
            meta, blocks = asbuilt.load_backup(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "Hatchling", f"Could not read backup: {e}")
            return
        key = f"{meta['module']['bus']}:{meta['module']['addr']}"
        if self.module is None or self.module.key != key:
            QMessageBox.information(self, "Hatchling",
                                    f"This backup is for {meta['module']['name']} ({key}). "
                                    "Select and read that module first, then load the backup.")
            return
        self._loading = True
        for r, did in enumerate(self.dids):
            data = blocks.get(did)
            if data is None or len(data) != len(self.original[did]):
                continue
            for c, b in enumerate(data):
                self.tbl.item(r, c + 1).setText(f"{b:02X}")
        self._loading = False
        self.recolor()
        n = len(asbuilt.diff(self.original, self.edited_blocks()))
        self.s.log(f"Loaded backup {os.path.basename(path)} - {n} byte(s) differ from the module")

    def put_blocks(self, blocks: asbuilt.Blocks) -> Tuple[int, List[str]]:
        """Loads blocks into the editor (not the module). Returns (bytes changed, skipped blocks)."""
        skipped = []
        self._loading = True
        for r, did in enumerate(self.dids):
            data = blocks.get(did)
            if data is None:
                continue
            if len(data) != len(self.original[did]):
                skipped.append(f"{did:04X} ({len(data)} bytes, module has {len(self.original[did])})")
                continue
            for c, b in enumerate(data):
                self.tbl.item(r, c + 1).setText(f"{b:02X}")
        self._loading = False
        self.recolor()
        return len(asbuilt.diff(self.original, self.edited_blocks())), skipped

    def import_text(self):
        """Paste As-Built lines (e.g. from a factory As-Built record or another car) into the editor."""
        if not self.module:
            QMessageBox.information(self, "Hatchling", "Select and read the module first, then import.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Import As-Built")
        dlg.setMinimumSize(560, 420)
        lay = QVBoxLayout(dlg)
        info = QLabel(f"Paste lines like <code>{self.module.addr:03X}-01-01 2A80 40A6 CB</code>. "
                      "Only lines for this module are used. Nothing is written until you press Write changes.")
        info.setWordWrap(True)
        lay.addWidget(info)
        box = QPlainTextEdit()
        box.setStyleSheet("font-family: Consolas, monospace;")
        lay.addWidget(box, 1)
        cks = QCheckBox("Each line ends with a checksum byte (remove it)")
        cks.setChecked(True)
        lay.addWidget(cks)
        row = QHBoxLayout()
        row.addStretch()
        cancel, ok = button("Cancel"), button("Load into editor", "primary")
        cancel.clicked.connect(dlg.reject)
        ok.clicked.connect(dlg.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        if not dlg.exec():
            return
        res = asbuilt.parse_text(box.toPlainText(), cks.isChecked())
        blocks = res.modules.get(self.module.addr)
        if not blocks:
            found = ", ".join(f"{a:03X}" for a in res.modules) or "none"
            QMessageBox.warning(self, "Hatchling", f"No lines for module {self.module.addr:03X} found "
                                                 f"(modules in the text: {found}).")
            return
        n, skipped = self.put_blocks(blocks)
        msg = f"Loaded {len(blocks)} block(s) - {n} byte(s) differ from the module (highlighted)."
        if cks.isChecked() and res.lines:
            msg += f"<br>Checksums matched on {res.checksum_matches} of {res.lines} line(s)."
            if res.checksum_matches == 0:
                msg += f"<br><span style='color:{WARN}'>None matched - if the lengths look wrong, re-import " \
                       "with the checksum box unticked.</span>"
        if skipped:
            msg += "<br><br>Skipped (length mismatch):<br>" + "<br>".join(skipped)
        self.s.log(f"Imported As-Built text for {self.module.name}: {n} byte(s) differ")
        QMessageBox.information(self, "Import As-Built", msg)

    # ---- feature finder ------------------------------------------------
    def make_feature(self):
        """Turns the highlighted edits (e.g. from a loaded backup of a car that has the feature)
        into a feature file - 'off' is what the module has now, 'on' is what's in the editor."""
        if not self.module:
            QMessageBox.information(self, "Hatchling", "Read a module first, then load a backup, import As-Built "
                                                    "text or edit bytes so the differences are highlighted.")
            return
        changes = finder.compare(self.original, self.edited_blocks())
        if not changes:
            QMessageBox.information(self, "Hatchling", "No differences yet. Load a backup from a car that has the "
                                                    "feature (or edit the bits) so they're highlighted first.")
            return
        FeatureFinderDialog(self, self.s, [(self.module, changes)], before_label="Module now",
                            after_label="Editor").exec()

    def compare_factory(self):
        """Loads the factory values for this module into the editor so every change since the
        factory is highlighted. Write changes would put the module back to factory settings."""
        if not self.module:
            QMessageBox.information(self, "Hatchling", "Read a module first.")
            return
        if not self.s.factory:
            if QMessageBox.question(self, "Hatchling", "No factory As-Built for this car yet. Import Ford's "
                                    "As-Built file now?") == QMessageBox.Yes and import_factory_file(self, self.s):
                self.compare_factory()
            return
        fb = factory.align(self.s.factory.blocks(self.module.addr), self.original)
        if not fb:
            QMessageBox.information(self, "Hatchling", f"The factory file has no data for module "
                                                       f"{self.module.addr:03X}.")
            return
        n, skipped = self.put_blocks(fb)
        self.s.log(f"{self.module.name}: {n} byte(s) differ from factory")
        QMessageBox.information(self, "Compared with factory",
                                f"{n} byte(s) differ from how this module left the factory (highlighted).<br><br>"
                                "<b>Make feature from edits</b> and tick <i>My car already has this feature</i> to "
                                "save a change you made as a feature. <b>Write changes</b> would put these bytes "
                                "back to factory - <b>Undo edits</b> to leave the module as it is.")

    def compare_backups(self):
        a, _ = QFileDialog.getOpenFileName(self, "Backup WITHOUT the feature (before)", asbuilt.BACKUP_DIR,
                                           "Hatchling backup (*.json)")
        if not a:
            return
        b, _ = QFileDialog.getOpenFileName(self, "Backup WITH the feature (after)", os.path.dirname(a),
                                           "Hatchling backup (*.json)")
        if not b:
            return
        try:
            ma, ba = asbuilt.load_backup(a)
            mb, bb = asbuilt.load_backup(b)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "Hatchling", f"Could not read backup: {e}")
            return
        if ma["module"]["addr"] != mb["module"]["addr"]:
            if QMessageBox.question(self, "Hatchling", f"These backups are from different modules "
                                    f"({ma['module']['addr']} and {mb['module']['addr']}). Compare anyway?") \
                    != QMessageBox.Yes:
                return
        changes = finder.compare(ba, bb)
        if not changes:
            QMessageBox.information(self, "Hatchling", "The two backups are identical.")
            return
        m = Module(int(ma["module"]["addr"], 16), ma["module"]["bus"], ma["module"]["name"])
        FeatureFinderDialog(self, self.s, [(m, changes)], before_label="First backup",
                            after_label="Second backup").exec()

    def write(self):
        if not self.module or not self.need_connection():
            return
        target = self.edited_blocks()
        changes = asbuilt.diff(self.original, target)
        if not changes:
            QMessageBox.information(self, "Hatchling", "No changes to write.")
            return
        lines = "".join(f"<tr><td>{asbuilt.did_name(d)}</td><td>byte {i}</td><td>{a:02X} → {b:02X}</td></tr>"
                        for d, i, a, b in changes)
        msg = (f"<b>Write {len(changes)} change(s) to {self.module.name}?</b><br><br>"
               f"<table cellspacing=6>{lines}</table><br>"
               "A backup is saved first. Keep the ignition on and a charger connected until it finishes.")
        if QMessageBox.question(self, "Confirm write", msg) != QMessageBox.Yes:
            return
        m, orig = self.module, self.original

        def ok(res: asbuilt.WriteResult):
            self.s.record(history.WRITE, f"{m.name}: {len(changes)} byte(s) - {res.message}",
                          {"module": f"{m.addr:03X}", "bus": m.bus, "backup": res.backup_path,
                           "changes": [[f"{d:04X}", i, a, b] for d, i, a, b in changes]})
            (QMessageBox.information if res.verified else QMessageBox.critical)(self, "Write result",
                                                                             f"{res.message}<br><br>Backup: {res.backup_path}")
            self.read()
        self.s.ensure_bus(m.bus, lambda: self.s.runner.run(
            lambda: asbuilt.safe_write(self.s.uds, m, orig, target, self.s.vin, self.s.log), ok))


# ---------------------------------------------------------------- Dashboard
class Gauge(QWidget):
    """Round dial: 240° sweep, major ticks, red zone in the top 15%, big centred value."""

    def __init__(self):
        super().__init__()
        self.setMinimumSize(220, 220)
        self.sig: Optional[livedata.Signal] = None
        self.value: Optional[float] = None
        self.shown = 0.0

    def set_signal(self, sig):
        self.sig, self.value, self.shown = sig, None, sig.lo if sig else 0.0
        self.update()

    def set_value(self, v: Optional[float]):
        self.value = v
        if v is not None:
            self.shown += (v - self.shown) * 0.5   # light smoothing so the needle doesn't jump
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 12
        r = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        c = r.center()
        rad = side / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(PANEL_ALT))
        p.drawEllipse(r)
        if not self.sig:
            p.setPen(QColor(FAINT))
            p.drawText(r, Qt.AlignCenter, "Pick a value")
            return
        lo, hi = self.sig.lo, self.sig.hi
        span = (hi - lo) or 1
        arc = r.adjusted(side * 0.08, side * 0.08, -side * 0.08, -side * 0.08)
        # track, red zone, value arc (Qt angles: 1/16 deg, 0 = 3 o'clock, positive = counter-clockwise)
        p.setPen(QPen(QColor(LINE), side * 0.05, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(arc, 210 * 16, -240 * 16)
        p.setPen(QPen(QColor(DANGER), side * 0.05, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(arc, int((210 - 240 * 0.85) * 16), int(-240 * 0.15 * 16))
        frac = max(0.0, min(1.0, (self.shown - lo) / span))
        p.setPen(QPen(QColor(ACCENT), side * 0.05, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(arc, 210 * 16, int(-240 * frac * 16))
        # ticks + labels
        p.setFont(QFont("Segoe UI", max(7, int(side / 30))))
        for i in range(11):
            ang = math.radians(210 - 24 * i)
            outer, inner = rad * 0.80, rad * (0.72 if i % 2 == 0 else 0.76)
            p.setPen(QPen(QColor(MUTED), 2 if i % 2 == 0 else 1))
            p.drawLine(QPointF(c.x() + outer * math.cos(ang), c.y() - outer * math.sin(ang)),
                       QPointF(c.x() + inner * math.cos(ang), c.y() - inner * math.sin(ang)))
            if i % 2 == 0:
                val = lo + span * i / 10
                lab = f"{val / 1000:g}k" if abs(hi) >= 2000 else f"{val:g}"
                lr = rad * 0.60
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(c.x() + lr * math.cos(ang) - 22, c.y() - lr * math.sin(ang) - 9, 44, 18),
                           Qt.AlignCenter, lab)
        # needle
        ang = math.radians(210 - 240 * frac)
        p.setPen(QPen(QColor(INK), max(2, side / 90), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c, QPointF(c.x() + rad * 0.70 * math.cos(ang), c.y() - rad * 0.70 * math.sin(ang)))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT))
        p.drawEllipse(c, side * 0.03, side * 0.03)
        # value + name
        f = QFont("Segoe UI", max(12, int(side / 9)))
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(INK))
        txt = "-" if self.value is None else (f"{self.value:.0f}" if abs(self.value) >= 100 else f"{self.value:.1f}")
        p.drawText(QRectF(r.x(), c.y() + side * 0.12, r.width(), side * 0.16), Qt.AlignCenter, txt)
        p.setFont(QFont("Segoe UI", max(8, int(side / 24))))
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(r.x(), c.y() + side * 0.28, r.width(), side * 0.1), Qt.AlignCenter,
                   f"{self.sig.name} ({self.sig.unit})" if self.sig.unit else self.sig.name)


class DashboardPage(Page):
    DEFAULTS = ("obd:0C", "obd:0D", "obd:05", "obd:42")

    def __init__(self, s):
        super().__init__(s, "Gauges", "Four dials for any reading. Full screen works well on a laptop on the passenger "
                                         "seat - press Esc to come back.")
        row = QHBoxLayout()
        self.btn = button("Start", "primary")
        self.btn.clicked.connect(self.toggle)
        self.fs_btn = button("Full screen")
        self.fs_btn.clicked.connect(self.toggle_full)
        row.addWidget(self.btn)
        row.addWidget(self.fs_btn)
        row.addStretch()
        self.root.addLayout(row)
        grid = QGridLayout()
        grid.setSpacing(14)
        self.gauges, self.pickers = [], []
        for i in range(4):
            box = QVBoxLayout()
            g = Gauge()
            pick = QComboBox()
            pick.currentIndexChanged.connect(lambda _, i=i: self.pick(i))
            box.addWidget(g, 1)
            box.addWidget(pick)
            self.gauges.append(g)
            self.pickers.append(pick)
            grid.addLayout(box, i // 2, i % 2)
        self.root.addLayout(grid, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.poll)
        self.polling = False
        self.full = False
        self.esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.esc.activated.connect(lambda: self.toggle_full() if self.full else None)
        self.fill()

    def fill(self):
        self.signals = {sg.key: sg for sg in livedata.load_all()}
        for i, pick in enumerate(self.pickers):
            pick.blockSignals(True)
            pick.clear()
            for sg in self.signals.values():
                pick.addItem(f"{sg.name} · {sg.group}", sg.key)
            idx = pick.findData(self.DEFAULTS[i])
            pick.setCurrentIndex(max(0, idx))
            pick.blockSignals(False)
            self.pick(i)

    def pick(self, i: int):
        self.gauges[i].set_signal(self.signals.get(self.pickers[i].currentData()))

    def toggle(self):
        if self.timer.isActive():
            self.timer.stop()
            self.btn.setText("Start")
            return
        if not self.need_connection():
            return
        buses = {g.sig.bus for g in self.gauges if g.sig and g.sig.kind == "did"}
        if len(buses) > 1:
            QMessageBox.information(self, "Hatchling", "These dials are on different buses. Pick values from one bus "
                                                    "(engine values count as HS-CAN).")
            return
        target = buses.pop() if buses else (OBD if self.s.obd_only else HS)
        self.s.ensure_bus(target, self._start)

    def _start(self):
        self.timer.start()
        self.btn.setText("Stop")

    def poll(self):
        if self.polling or self.s.runner.busy or not self.s.connected:
            return
        sigs = [g.sig for g in self.gauges]
        self.polling = True

        def job():
            out = []
            for sg in sigs:
                try:
                    if sg is None:
                        out.append(None)
                    elif sg.kind == "obd" and self.s.elm.bus == OBD:
                        out.append(sg.formula(self.s.obd.pid(sg.ident)))
                    else:
                        out.append(livedata.read(self.s.uds, sg))
                except (UdsError, ElmError, IndexError, ZeroDivisionError, ValueError):
                    out.append(None)
            return out

        def ok(vals):
            self.polling = False
            for g, v in zip(self.gauges, vals):
                g.set_value(v)

        def err(e):
            self.polling = False
            self.timer.stop()
            self.btn.setText("Start")
            self.s.log(f"Dashboard stopped: {e}")
        self.s.runner.run(job, ok, err)

    def toggle_full(self):
        self.full = not self.full
        self.s.focus_mode(self.full)
        for pick in self.pickers:
            pick.setVisible(not self.full)
        self.fs_btn.setText("Exit full screen" if self.full else "Full screen")

    def hideEvent(self, e):
        self.timer.stop()
        self.btn.setText("Start")
        super().hideEvent(e)


# ---------------------------------------------------------------- History
class HistoryPage(Page):
    def __init__(self, s):
        super().__init__(s, "History", "A record of everything Hatchling has done on each car, so you can see what "
                                       "changed and when.")
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.cars = QListWidget()
        self.cars.setMinimumWidth(240)
        self.cars.currentRowChanged.connect(self.show_car)
        ll.addWidget(self.cars, 1)
        del_btn = button("Delete this car's history")
        del_btn.clicked.connect(self.delete_car)
        ll.addWidget(del_btn)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 0, 0, 0)
        top = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItem("Everything", None)
        for k, label in history.KIND_LABELS.items():
            self.filter.addItem(label, k)
        self.filter.currentIndexChanged.connect(self.show_car)
        note_btn = button("Add note")
        note_btn.clicked.connect(self.add_note)
        top.addWidget(QLabel("Show"))
        top.addWidget(self.filter)
        top.addStretch()
        top.addWidget(note_btn)
        rl.addLayout(top)
        self.changes = QLabel("")
        self.changes.setWordWrap(True)
        rl.addWidget(self.changes)
        self.tbl = table(["When", "What", "Details"])
        self.tbl.currentCellChanged.connect(lambda r, *_: self.show_detail(r))
        rl.addWidget(self.tbl, 2)
        self.detail = QTextBrowser()
        rl.addWidget(self.detail, 1)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        self.root.addWidget(split, 1)
        self.events: List[history.Event] = []
        s.history_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        if not self.s.history:
            self.changes.setText("History isn't available (couldn't open the history file).")
            return
        cur = self.cars.currentItem().data(Qt.UserRole) if self.cars.currentItem() else self.s.vin
        self.cars.blockSignals(True)
        self.cars.clear()
        for v, ts, n in self.s.history.vins():
            info = vin.decode(v)
            it = QListWidgetItem(f"{info.summary if info else v}\n     {v} · {n} entries · "
                                 f"{time.strftime('%d %b %Y', time.localtime(ts))}")
            it.setData(Qt.UserRole, v)
            self.cars.addItem(it)
        self.cars.blockSignals(False)
        rows = [self.cars.item(i).data(Qt.UserRole) for i in range(self.cars.count())]
        self.cars.setCurrentRow(rows.index(cur) if cur in rows else 0 if rows else -1)
        self.show_car()

    def current_vin(self) -> Optional[str]:
        it = self.cars.currentItem()
        return it.data(Qt.UserRole) if it else None

    def show_car(self, *_):
        v = self.current_vin()
        self.tbl.setRowCount(0)
        self.detail.clear()
        if not v or not self.s.history:
            self.changes.setText(f"<span style='color:{MUTED}'>Nothing recorded yet - connect to a car to start.</span>")
            return
        kind = self.filter.currentData()
        self.events = self.s.history.events(v, [kind] if kind else None)
        for e in self.events:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            col = {history.CODES: WARN, history.FEATURE: ACCENT, history.WRITE: ACCENT, history.CLEAR: OK}.get(e.kind)
            self.tbl.setItem(r, 0, item(e.when))
            self.tbl.setItem(r, 1, item(e.label, col))
            self.tbl.setItem(r, 2, item(e.summary))
        ch = self.s.history.code_changes(v)
        if ch:
            bits = []
            if ch["new"]:
                bits.append(f"<span style='color:{DANGER}'>new: {', '.join(ch['new'])}</span>")
            if ch["gone"]:
                bits.append(f"<span style='color:{OK}'>gone: {', '.join(ch['gone'])}</span>")
            if ch["still"]:
                bits.append(f"still there: {', '.join(ch['still'])}")
            self.changes.setText(f"<b>Fault codes since {ch['since']}:</b> " + (" · ".join(bits) or "no change"))
        else:
            self.changes.setText("")

    def show_detail(self, r: int):
        if not 0 <= r < len(self.events):
            return
        e = self.events[r]
        rows = []
        for k, val in e.data.items():
            if isinstance(val, list):
                if val and isinstance(val[0], dict):
                    val = "<br>".join(" · ".join(str(x) for x in d.values()) for d in val)
                else:
                    val = ", ".join(str(x) for x in val) or "none"
            rows.append(f"<tr><td style='color:{MUTED};padding-right:12px'>{k}</td><td>{val}</td></tr>")
        self.detail.setHtml(f"<b>{e.label}</b> · {e.when}<p>{e.summary}</p><table>{''.join(rows)}</table>")

    def add_note(self):
        v = self.current_vin() or self.s.vin
        if not v:
            QMessageBox.information(self, "Hatchling", "Connect to a car first, or pick one from the list.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Add note")
        lay = QVBoxLayout(dlg)
        box = QPlainTextEdit()
        box.setPlaceholderText("Replaced front-left wheel speed sensor")
        lay.addWidget(box)
        row = QHBoxLayout()
        row.addStretch()
        cancel, ok = button("Cancel"), button("Save note", "primary")
        cancel.clicked.connect(dlg.reject)
        ok.clicked.connect(dlg.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        if dlg.exec() and box.toPlainText().strip():
            self.s.history.add(v, history.NOTE, box.toPlainText().strip())
            self.s.history_changed.emit()

    def delete_car(self):
        v = self.current_vin()
        if not v:
            return
        if QMessageBox.question(self, "Delete history", f"Delete all history for {v}? This can't be undone.") \
                == QMessageBox.Yes:
            self.s.history.delete_vin(v)
            self.refresh()


# ---------------------------------------------------------------- Library
class LibrarySettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Feature library")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.addWidget(QLabel("GitHub repository that holds the library"))
        self.repo = QLineEdit(library.repo())
        self.repo.setPlaceholderText("owner/hatchling")
        lay.addWidget(self.repo)
        note = QLabel("Browsing needs no account. Sharing a feature or confirming one opens GitHub in your browser "
                      "with everything filled in - you'll need a free GitHub account to submit it.")
        note.setObjectName("subtle")
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QHBoxLayout()
        row.addStretch()
        cancel, ok = button("Cancel"), button("Save", "primary")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.save)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def save(self):
        try:
            r = library.check_repo(self.repo.text()) if self.repo.text().strip() else ""
        except library.LibraryError as e:
            QMessageBox.warning(self, "Hatchling", str(e))
            return
        cfg = library.settings()
        cfg["library_repo"] = r
        library.save_settings(cfg)
        self.accept()


def open_url(url: str):
    import webbrowser
    webbrowser.open(url)


class LibraryPage(Page):
    def __init__(self, s):
        super().__init__(s, "Feature library", "Features other owners have worked out on the same car. Install one, "
                                       "then turn it on from the Features page.")
        row = QHBoxLayout()
        self.refresh_btn = button("Check for new features", "primary")
        self.install_btn = button("Install")
        self.only_mine = QCheckBox("Only features for my car's modules")
        self.only_mine.setChecked(True)
        settings_btn = button("Library settings")
        self.refresh_btn.clicked.connect(self.refresh)
        self.install_btn.clicked.connect(self.install)
        self.only_mine.toggled.connect(self.render)
        settings_btn.clicked.connect(self.edit_settings)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.install_btn)
        row.addSpacing(12)
        row.addWidget(self.only_mine)
        row.addStretch()
        row.addWidget(settings_btn)
        self.root.addLayout(row)
        self.status = QLabel("")
        self.status.setObjectName("subtle")
        self.status.setWordWrap(True)
        self.root.addWidget(self.status)
        split = QSplitter(Qt.Vertical)
        self.tbl = table(["Feature", "Category", "Learned on", "Your modules", "Confirmed", "Installed"])
        self.tbl.currentCellChanged.connect(lambda r, *_: self.show_detail(r))
        self.tbl.itemDoubleClicked.connect(lambda _: self.install())
        split.addWidget(self.tbl)
        self.detail = QTextBrowser()
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.root.addWidget(split, 1)
        self.index, self.source = library.best_index()
        self.shown: List[dict] = []
        s.modules_changed.connect(self.render)
        s.connected_changed.connect(lambda _: self.render())
        self.render()
        QTimer.singleShot(4000, lambda: self.refresh(quiet=True))   # quietly check GitHub at startup

    def items(self) -> List[dict]:
        make = self.s.vin_info.make if self.s.vin_info else None
        return [e for e in (self.index or {}).get("features", []) if library.entry_matches_make(e, make)]

    def show_status(self, extra: str = ""):
        if not self.index:
            txt = "No library yet."
        else:
            when = self.index.get("generated", "")[:10]
            txt = f"Library from {when} ({self.source})"
        if not library.repo():
            txt += f" <span style='color:{WARN}'>Set the GitHub repository in Library settings to get new features.</span>"
        self.status.setText(txt + (f" · {extra}" if extra else ""))

    def edit_settings(self):
        if LibrarySettingsDialog(self).exec():
            self.refresh()

    def refresh(self, quiet: bool = False):
        if not library.repo():
            self.show_status()
            if not quiet:
                QMessageBox.information(self, "Hatchling", "Set the library's GitHub repository first "
                                                           "(Library settings).")
            return

        def ok(idx):
            new = len(idx["features"]) - len((self.index or {}).get("features", []))
            self.index, self.source = idx, "downloaded"
            self.render()
            self.s.log(f"Library updated: {len(idx['features'])} feature(s)")
            if not quiet:
                self.show_status("up to date" + (f", {new} new" if new > 0 else ""))

        def err(e):
            self.show_status(f"<span style='color:{WARN}'>offline - {e}</span>" if not quiet else "")
        self.s.runner.run(library.update, ok, err)

    def match(self, entry: dict) -> Tuple[str, str]:
        if not self.s.modules:
            return "", "scan to check"
        f = features._parse({"name": entry["name"], "modules": [dict(m, changes=[]) for m in entry["modules"]]},
                            entry["id"])
        level = features.compatibility_level(
            features.compatibility(f, {(m.bus, m.addr): m.identity for m in self.s.modules.values()}))
        return level, {features.SAME: "match", features.FAMILY: "same, other revision",
                       features.DIFFERENT: "different modules", features.MISSING: "module not on car",
                       features.UNKNOWN_PART: "can't compare"}[level]

    def render(self):
        have = library.installed()
        items = self.items()
        self.shown = []
        self.tbl.setRowCount(0)
        for e in items:
            level, text = self.match(e)
            if self.only_mine.isChecked() and self.s.modules and level in (features.DIFFERENT, features.MISSING):
                continue
            self.shown.append(e)
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            col = {features.SAME: OK, features.FAMILY: OK, features.DIFFERENT: DANGER,
                   features.MISSING: WARN}.get(level)
            n = e.get("confirmations", 0)
            vals = [e["name"], e.get("category", ""), e.get("learned_on", ""), text,
                    f"{n} car{'s' if n != 1 else ''}", "yes" if e["id"] in have else ""]
            for c, v in enumerate(vals):
                self.tbl.setItem(r, c, item(v, col if c == 3 else ACCENT if c == 5 and v else None))
        hidden = len(items) - len(self.shown)
        self.show_status(f"{len(self.shown)} feature(s)" + (f", {hidden} hidden for other modules" if hidden else ""))
        if not self.shown:
            self.detail.setHtml(f"<p style='color:{MUTED}'>No features for this car yet. Once you've learned one "
                                "on your car, use <b>Share to library</b> on the Features page.</p>")
        else:
            self.tbl.setCurrentCell(0, 0)

    def show_detail(self, r: int):
        if not 0 <= r < len(self.shown):
            return
        e = self.shown[r]
        mine = {(m.bus, m.addr): m.identity.get("Part number", "") for m in self.s.modules.values()}
        rows = ""
        for m in e["modules"]:
            addr = int(m["module"], 16)
            car = mine.get((m["bus"], addr), "")
            rows += (f"<tr><td>{m.get('label') or m['module']}</td><td>{m['bus']} {addr:03X}</td>"
                     f"<td>{m.get('part') or '-'}</td><td>{car or ('-' if self.s.modules else 'scan to check')}</td></tr>")
        fam = e.get("confirmations_by_family") or {}
        conf = ", ".join(f"{k}: {v}" for k, v in fam.items()) or "none yet"
        self.detail.setHtml(
            f"<h3>{e['name']}</h3><p>{e.get('description') or ''}</p>"
            f"<p><b>Hardware needed:</b> {e.get('hardware_required') or 'none listed'}<br>"
            f"<b>Learned on:</b> {e.get('learned_on') or '-'} · updated {e.get('updated', '')}<br>"
            f"<b>Confirmed working, by module:</b> {conf}</p>"
            f"<table cellspacing=6><tr><th align=left>Module</th><th align=left>Where</th>"
            f"<th align=left>Learned on part</th><th align=left>Your part</th></tr>{rows}</table>")

    def install(self):
        r = self.tbl.currentRow()
        if not 0 <= r < len(self.shown):
            return
        e = self.shown[r]
        level, _ = self.match(e)
        if level == features.DIFFERENT and QMessageBox.warning(
                self, "Different modules", f"Your modules are different from the ones <b>{e['name']}</b> was learned "
                "on. The same bits may do something else on your car.<br><br>Install anyway?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        def ok(data):
            try:
                path = library.install(data)
            except (library.LibraryError, OSError, ValueError, KeyError) as ex:
                QMessageBox.warning(self, "Hatchling", f"Couldn't install it: {ex}")
                return
            self.s.log(f"Installed from library: {e['name']} -> {path}")
            self.s.features_changed.emit()
            self.render()
            QMessageBox.information(
                self, "Installed", f"<b>{e['name']}</b> is in your Features list.<br><br>Run a <b>module scan</b> to "
                "see whether it's on, then switch it on from <b>Features</b>.<br><br>Once it works on your car, "
                "press <b>Mark as working on my car</b>.")
        self.s.runner.run(lambda: library.get(e), ok)


# ---------------------------------------------------------------- Service
class ServicePage(Page):
    RESTART = "__restart__"

    def __init__(self, s):
        super().__init__(s, "Service jobs", "Guided jobs like resetting the battery monitor or calibrating the steering "
                                       "angle. You'll see every step before anything runs.")
        split = QSplitter()
        self.list = QListWidget()
        self.list.setMinimumWidth(250)
        self.list.currentRowChanged.connect(self.show_proc)
        split.addWidget(self.list)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 0, 0, 0)
        rl.setSpacing(10)
        head, hl = card()
        self.title = QLabel("")
        self.title.setObjectName("title")
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("subtle")
        self.meta.setWordWrap(True)
        hl.addWidget(self.title)
        hl.addWidget(self.desc)
        hl.addWidget(self.meta)
        rl.addWidget(head)
        self.restart_row = QHBoxLayout()
        self.restart_mod = QComboBox()
        self.restart_mod.setMinimumWidth(280)
        self.restart_row.addWidget(QLabel("Module"))
        self.restart_row.addWidget(self.restart_mod)
        self.restart_row.addStretch()
        rl.addLayout(self.restart_row)
        steps_lbl = QLabel("Steps")
        steps_lbl.setObjectName("subtle")
        rl.addWidget(steps_lbl)
        self.steps = QListWidget()
        rl.addWidget(self.steps, 1)
        self.confirm = QCheckBox("I've checked this procedure is right for my car")
        rl.addWidget(self.confirm)
        row = QHBoxLayout()
        self.run_btn = button("Run procedure", "primary")
        self.stop_btn = button("Stop")
        reload_btn = button("Reload files")
        self.run_btn.clicked.connect(self.run)
        self.stop_btn.clicked.connect(lambda: setattr(self, "cancel", True))
        reload_btn.clicked.connect(self.reload)
        row.addWidget(self.run_btn)
        row.addWidget(self.stop_btn)
        row.addStretch()
        row.addWidget(reload_btn)
        rl.addLayout(row)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        self.root.addWidget(split, 1)
        self.cancel = False
        s.modules_changed.connect(self.fill_modules)
        self.reload()

    def reload(self):
        self.procs = services.load_all()
        self.list.clear()
        QListWidgetItem("Restart a module\n     General", self.list)
        for p in self.procs:
            QListWidgetItem(f"{p.name}\n     {p.category}", self.list)
        self.list.setCurrentRow(0)

    def fill_modules(self):
        self.restart_mod.clear()
        for m in sorted(self.s.modules.values(), key=lambda m: (m.bus, m.addr)):
            self.restart_mod.addItem(m.label, m.key)

    def proc(self):
        r = self.list.currentRow()
        if r == 0:
            return self.RESTART
        return self.procs[r - 1] if 0 < r <= len(self.procs) else None

    def show_proc(self, *_):
        p = self.proc()
        self.steps.clear()
        restart = p == self.RESTART
        for i in range(self.restart_row.count()):
            w = self.restart_row.itemAt(i).widget()
            if w:
                w.setVisible(restart)
        if restart:
            self.title.setText("Restart a module")
            self.desc.setText("Power-cycles one module, the same as unplugging it. Useful after configuration "
                              "changes or when a module stops responding.")
            self.meta.setText("Engine off. Restarting the PCM or ABS while driving is unsafe.")
            for t in ("Confirm the engine is off", "Send reset to the module", "Wait for it to come back"):
                QListWidgetItem(f"○  {t}", self.steps)
            self.confirm.hide()
            return
        if not p:
            return
        self.title.setText(p.name)
        self.desc.setText(p.description)
        verified = (f"<span style='color:{OK}'>Verified</span>" if p.verified
                    else f"<span style='color:{WARN}'>Not verified - check every step yourself</span>")
        pre = "".join(f"<br>• {c}" for c in p.preconditions)
        self.meta.setText(f"{verified} · {p.module_label or f'Module {p.module:03X}'} ({p.bus}-CAN {p.module:03X})"
                          f"<br>Vehicle: {p.vehicle}{'<br><b>Before you start:</b>' + pre if pre else ''}")
        for st in p.steps:
            QListWidgetItem(f"○  {services.describe(st)}", self.steps)
        self.confirm.setVisible(not p.verified)
        self.confirm.setChecked(False)

    def mark(self, i: int, state: str, extra: str = ""):
        it = self.steps.item(i)
        if not it:
            return
        icon, col = {"run": ("◔", ACCENT), "ok": ("✓", OK), "fail": ("✗", DANGER)}[state]
        text = it.text()[3:].split("  —  ")[0]
        it.setText(f"{icon}  {text}" + (f"  —  {extra}" if extra else ""))
        it.setForeground(QColor(col))

    def run(self):
        p = self.proc()
        if not p or not self.need_connection():
            return
        if p == self.RESTART:
            self.run_restart()
            return
        if not p.verified and not self.confirm.isChecked():
            QMessageBox.warning(self, "Hatchling", "Tick the confirmation box once you've checked this procedure.")
            return
        self.show_proc()
        self.cancel = False
        self.s.log(f"Service: {p.name} started")
        self.s.ensure_bus(p.bus, lambda: self._step(p, 0))

    def _step(self, p: services.Procedure, i: int):
        if i >= len(p.steps):
            self.s.log(f"Service: {p.name} finished")
            self.s.record(history.SERVICE, f"{p.name} completed", {"procedure": p.id})
            SuccessDialog(self, f"{p.name} complete", ["All steps finished."]).exec()
            return
        if self.cancel:
            self.mark(i, "fail", "stopped")
            self.s.log(f"Service: {p.name} stopped by user")
            return
        st = p.steps[i]
        self.mark(i, "run")
        if st.type == "prompt":
            r = QMessageBox.question(self, p.name, st.args.get("text", "Continue?"),
                                     QMessageBox.Ok | QMessageBox.Cancel)
            if r != QMessageBox.Ok:
                self.mark(i, "fail", "cancelled")
                return
            self.mark(i, "ok")
            self._step(p, i + 1)
            return

        def ok(res):
            self.mark(i, "ok", res)
            self._step(p, i + 1)

        def err(e):
            self.mark(i, "fail", str(e))
            self.s.log(f"Service: {p.name} failed at step {i + 1}: {e}")
            self.s.record(history.SERVICE, f"{p.name} failed at step {i + 1}: {e}", {"procedure": p.id})
            QMessageBox.warning(self, p.name, f"Step {i + 1} failed:<br>{e}<br><br>Nothing after this step was run.")
        self.s.runner.run(lambda: services.run_step(self.s.uds, p, st, self.s.log, lambda: self.cancel), ok, err)

    def run_restart(self):
        m = self.s.modules.get(self.restart_mod.currentData())
        if not m:
            QMessageBox.information(self, "Hatchling", "Scan for modules first.")
            return
        if QMessageBox.question(self, "Restart module", f"Restart <b>{m.name}</b>?<br><br>Engine must be off.") \
                != QMessageBox.Yes:
            return
        self.mark(0, "ok")
        self.mark(1, "run")

        def job():
            self.s.uds.ecu_reset(m.addr, 0x01)
            for _ in range(20):
                time.sleep(0.5)
                if self.s.uds.tester_present(m.addr):
                    return True
            return False

        def ok(back):
            self.mark(1, "ok")
            self.mark(2, "ok" if back else "fail", "" if back else "no answer yet - give it a minute")
            self.s.log(f"{m.name} restarted" + ("" if back else " (not answering yet)"))
            self.s.record(history.SERVICE, f"Restarted {m.name}", {"module": f"{m.addr:03X}"})

        def err(e):
            self.mark(1, "fail", str(e))
        self.s.ensure_bus(m.bus, lambda: self.s.runner.run(job, ok, err))


# ---------------------------------------------------------------- Features
class Toggle(QCheckBox):
    """On/off switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(46, 24)

    def hitButton(self, pos):
        return self.contentsRect().contains(pos)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        track = QColor(OK if on else LINE_STRONG)
        if not self.isEnabled():
            track = QColor(LINE)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(0, 2, 46, 20, 10, 10)
        p.setBrush(QColor("#FFFFFF" if self.isEnabled() else FAINT))
        p.drawEllipse(26 if on else 3, 4, 16, 16)


STATE_COLORS = {features.ENABLED: OK, features.DISABLED: MUTED, features.MIXED: WARN,
                features.UNKNOWN: DANGER, features.NEEDS_VALUES: WARN, features.NOT_FOUND: FAINT}


class PartRow(QFrame):
    def __init__(self, part: features.Part, changed: Callable[[], None], tech: bool = False):
        super().__init__()
        self.part = part
        self.setObjectName("card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        text = QVBoxLayout()
        name = QLabel(f"<b>{part.label}</b>")
        if not part.configured:
            detail = "The feature file doesn't say what to change here yet"
        elif tech:
            detail = (f"{part.bus}-CAN, module {part.module:03X}: " +
                      ", ".join(f"block {c.did:04X} byte {c.byte} mask {c.mask:02X}" for c in part.changes))
        else:
            sw = BUSES[part.bus].switch if part.bus in BUSES else part.bus
            detail = f"Adapter switch on {sw}"
        where = QLabel(detail)
        where.setObjectName("subtle")
        text.addWidget(name)
        text.addWidget(where)
        lay.addLayout(text, 1)
        self.state = QLabel("not read")
        self.state.setObjectName("subtle")
        lay.addWidget(self.state)
        lay.addSpacing(10)
        self.toggle = Toggle()
        self.toggle.setEnabled(False)
        self.toggle.toggled.connect(lambda _: changed())
        lay.addWidget(self.toggle)
        self.actual: Optional[str] = None

    def set_actual(self, state: str):
        self.actual = state
        self.toggle.blockSignals(True)
        self.toggle.setChecked(state == features.ENABLED)
        self.toggle.blockSignals(False)
        self.toggle.setEnabled(state in (features.ENABLED, features.DISABLED, features.MIXED))
        self.refresh()

    @property
    def pending(self) -> bool:
        if not self.toggle.isEnabled() or self.actual is None:
            return False
        if self.actual == features.MIXED:
            return True if self.toggle.isChecked() else False
        return self.toggle.isChecked() != (self.actual == features.ENABLED)

    def refresh(self):
        st = self.actual or "not read"
        col = STATE_COLORS.get(st, MUTED)
        extra = f" → <span style='color:{ACCENT}'>{'on' if self.toggle.isChecked() else 'off'}</span>" \
            if self.pending else ""
        self.state.setText(f"<span style='color:{col}'>{st}</span>{extra}")


class ManualChecksDialog(QDialog):
    """Asks the user to confirm the hardware checks only they can see."""

    def __init__(self, parent, feature: features.Feature, checks: List[features.HardwareCheck]):
        super().__init__(parent)
        self.setWindowTitle(f"{feature.name} - hardware")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        t = QLabel("Before enabling, confirm this hardware is fitted")
        t.setStyleSheet("font-size: 13pt; font-weight: 600;")
        lay.addWidget(t)
        self.boxes = []
        for c in checks:
            cb = QCheckBox(c.label)
            lay.addWidget(cb)
            if c.help:
                h = QLabel(c.help)
                h.setObjectName("subtle")
                h.setWordWrap(True)
                h.setContentsMargins(24, 0, 0, 4)
                lay.addWidget(h)
            self.boxes.append(cb)
        row = QHBoxLayout()
        row.addStretch()
        cancel = button("Not fitted")
        ok = button("Continue", "primary")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def answers(self) -> List[bool]:
        return [cb.isChecked() for cb in self.boxes]


class GuideDialog(QDialog):
    """One step of 'Enable in other modules'."""
    ENABLE, OPEN, SKIP = 1, 2, 3

    def __init__(self, parent, feature: features.Feature, part: features.Part, step: int, total: int,
                 current_bus: Optional[str]):
        super().__init__(parent)
        self.setWindowTitle(f"Enable {feature.name}")
        self.setMinimumWidth(500)
        self.choice = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(8)
        st = QLabel(f"Step {step} of {total}")
        st.setStyleSheet(f"color: {ACCENT}; font-weight: 600;")
        lay.addWidget(st)
        t = QLabel(part.label)
        t.setObjectName("title")
        lay.addWidget(t)
        where = QLabel(f"{part.bus}-CAN · module {part.module:03X}")
        where.setObjectName("subtle")
        lay.addWidget(where)
        c, cl = card()
        for ch in part.changes:
            cl.addWidget(QLabel(f"Block {ch.did:04X}, byte {ch.byte}: bits {ch.mask:02X} → {ch.on & ch.mask:02X}"))
        lay.addWidget(c)
        if current_bus and current_bus != part.bus:
            note = QLabel(f"This module is on {part.bus}-CAN - you'll be asked to flip the adapter switch.")
            note.setStyleSheet(f"color: {WARN};")
            note.setWordWrap(True)
            lay.addWidget(note)
        row = QHBoxLayout()
        b_open = button("Open in Configuration")
        b_skip = button("Skip")
        b_cancel = button("Stop")
        b_go = button("Enable this module", "primary")
        b_open.clicked.connect(lambda: self._done(self.OPEN))
        b_skip.clicked.connect(lambda: self._done(self.SKIP))
        b_cancel.clicked.connect(self.reject)
        b_go.clicked.connect(lambda: self._done(self.ENABLE))
        row.addWidget(b_open)
        row.addStretch()
        row.addWidget(b_cancel)
        row.addWidget(b_skip)
        row.addWidget(b_go)
        lay.addLayout(row)
        b_go.setDefault(True)

    def _done(self, choice):
        self.choice = choice
        self.accept()


class SuccessDialog(QDialog):
    def __init__(self, parent, title: str, lines: List[str]):
        super().__init__(parent)
        self.setWindowTitle("Hatchling")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(8)
        icon = QLabel("✓")
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(f"font-size: 40pt; color: {OK};")
        lay.addWidget(icon)
        t = QLabel(title)
        t.setAlignment(Qt.AlignCenter)
        t.setWordWrap(True)
        t.setStyleSheet("font-size: 16pt; font-weight: 600;")
        lay.addWidget(t)
        for l in lines:
            q = QLabel(l)
            q.setAlignment(Qt.AlignCenter)
            q.setWordWrap(True)
            q.setObjectName("subtle")
            lay.addWidget(q)
        ok = button("Done", "primary")
        ok.clicked.connect(self.accept)
        lay.addSpacing(6)
        lay.addWidget(ok, 0, Qt.AlignCenter)


class FeaturesPage(Page):
    def __init__(self, s):
        super().__init__(s, "Features", "Turn features on or off. Hatchling checks the parts are fitted first and "
                                        "backs everything up, so you can always switch back.")
        split = QSplitter()
        self.list = QListWidget()
        self.list.setMinimumWidth(250)
        self.list.currentRowChanged.connect(self.show_feature)
        split.addWidget(self.list)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 0, 0, 0)
        rl.setSpacing(10)
        head, hl = card()
        top = QHBoxLayout()
        tl = QVBoxLayout()
        self.title = QLabel("")
        self.title.setObjectName("title")
        self.overall = QLabel("")
        tl.addWidget(self.title)
        tl.addWidget(self.overall)
        top.addLayout(tl, 1)
        self.master = Toggle()
        self.master.setEnabled(False)
        self.master.clicked.connect(self.master_clicked)
        top.addWidget(self.master, 0, Qt.AlignTop)
        hl.addLayout(top)
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("subtle")
        self.meta.setWordWrap(True)
        hl.addWidget(self.desc)
        hl.addWidget(self.meta)
        rl.addWidget(head)

        # partly-enabled banner
        self.banner, bl = card()
        self.banner.setStyleSheet(f"QFrame#card {{ border-color: {WARN}; }}")
        brow = QHBoxLayout()
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        brow.addWidget(self.banner_text, 1)
        self.guide_btn = button("Enable in other modules →", "primary")
        self.guide_btn.clicked.connect(self.start_guide)
        brow.addWidget(self.guide_btn)
        bl.addLayout(brow)
        self.banner.hide()
        rl.addWidget(self.banner)

        # hardware
        hw, hwl = card()
        hrow = QHBoxLayout()
        hl2 = QLabel("<b>Hardware check</b>")
        hrow.addWidget(hl2)
        hrow.addStretch()
        self.hw_btn = button("Check hardware")
        self.hw_btn.clicked.connect(lambda: self.check_hardware())
        hrow.addWidget(self.hw_btn)
        hwl.addLayout(hrow)
        self.hw_list = QLabel("")
        self.hw_list.setWordWrap(True)
        hwl.addWidget(self.hw_list)
        rl.addWidget(hw)

        mrow = QHBoxLayout()
        lbl = QLabel("Where it's set")
        lbl.setObjectName("subtle")
        mrow.addWidget(lbl)
        mrow.addStretch()
        self.tech_box = QCheckBox("Show technical details")
        self.tech_box.toggled.connect(self.set_tech)
        mrow.addWidget(self.tech_box)
        rl.addLayout(mrow)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(8)
        rl.addLayout(self.rows_box)
        rl.addStretch()

        self.confirm = QCheckBox("I've checked these values are right for my car")
        rl.addWidget(self.confirm)
        row = QHBoxLayout()
        self.read_btn = button("Check what's on")
        self.apply_btn = button("Apply changes", "primary")
        self.verify_btn = button("Mark as working on my car")
        self.share_btn = button("Share to library")
        import_btn = button("Import feature")
        self.export_btn = button("Export")
        reload_btn = button("Reload files")
        self.read_btn.clicked.connect(lambda: self.read_status())
        self.apply_btn.clicked.connect(self.apply_changes)
        self.verify_btn.clicked.connect(self.mark_verified)
        self.share_btn.clicked.connect(self.share)
        import_btn.clicked.connect(self.import_feature)
        self.export_btn.clicked.connect(self.export_feature)
        reload_btn.clicked.connect(self.reload)
        row.addWidget(self.read_btn)
        row.addWidget(self.apply_btn)
        row.addWidget(self.verify_btn)
        row.addStretch()
        row.addWidget(self.share_btn)
        row.addWidget(import_btn)
        row.addWidget(self.export_btn)
        row.addWidget(reload_btn)
        rl.addLayout(row)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        self.root.addWidget(split, 1)

        self.feats: List[features.Feature] = []
        self.rows: List[PartRow] = []
        self.states: Dict[str, Dict[features.ModKey, str]] = {}   # feature id -> part states
        self.hw: Dict[str, List[Optional[Tuple[bool, str]]]] = {}  # feature id -> result per check
        self.blocks: Dict[features.ModKey, asbuilt.Blocks] = {}
        s.connected_changed.connect(lambda _: (self.states.clear(), self.hw.clear(), self.refresh_list(),
                                               self.show_feature()))
        s.features_changed.connect(self.reload)
        s.features_detected.connect(self.on_detected)
        self.reload()

    # ---- list -------------------------------------------------------
    def reload(self):
        self.feats = features.load_all()
        self.states.clear()
        self.hw.clear()
        self.list.clear()
        for _ in self.feats:
            QListWidgetItem("", self.list)
        self.refresh_list()
        if self.feats:
            self.list.setCurrentRow(0)
        else:
            self.title.setText("No features yet")
            self.desc.setText(f"Copy _TEMPLATE.json in {features.FEATURES_DIR} to create one.")

    def refresh_list(self):
        for i, f in enumerate(self.feats):
            st = self.feature_state(f)
            mark = {features.ENABLED: "●", features.DISABLED: "○", features.MIXED: "◐",
                    features.NOT_FOUND: "–"}.get(st, "·")
            it = self.list.item(i)
            if it is None:
                continue
            it.setText(f"{mark}  {f.name}\n     {f.category}")
            it.setForeground(QColor(STATE_COLORS.get(st, INK)))

    def feature(self) -> Optional[features.Feature]:
        r = self.list.currentRow()
        return self.feats[r] if 0 <= r < len(self.feats) else None

    def on_detected(self, payload):
        """Called after a module scan: states for every feature, read from the car."""
        self.blocks.update(payload["blocks"])
        self.states.update(payload["states"])
        self.refresh_list()
        self.show_feature()

    def feature_state(self, f: features.Feature) -> str:
        if not f.configured:
            return features.NEEDS_VALUES
        st = self.states.get(f.id)
        if not st:
            return "not read"
        return features.feature_state([st.get(p.key, features.UNKNOWN) for p in f.parts])

    def hardware_ok(self, f: features.Feature) -> Optional[bool]:
        """True all passed, False something failed, None not checked yet."""
        if not f.checks:
            return True
        res = self.hw.get(f.id)
        if not res or any(r is None for r in res):
            return None
        return all(r[0] for r in res)

    # ---- detail -----------------------------------------------------
    def show_feature(self, *_):
        f = self.feature()
        for r in self.rows:
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        if not f:
            return
        self.title.setText(f.name)
        self.desc.setText(f.description)
        verified = (f"<span style='color:{OK}'>Verified on this car</span>" if f.verified
                    else f"<span style='color:{WARN}'>Shared from another car - not tested on yours yet</span>"
                    if f.shared else f"<span style='color:{WARN}'>Not verified - check every value yourself</span>")
        self.meta.setText(f"{verified}<br>Vehicle: {f.vehicle or '-'}"
                          + (f" · learned on a {f.learned_on}" if f.learned_on and f.learned_on != f.vehicle else "")
                          + f"<br>Hardware needed: {f.hardware_required or 'none'}"
                          + (f"<br>Source: {f.source}" if f.source else "")
                          + self.compat_html(f))
        for p in f.parts:
            row = PartRow(p, self.update_pending, self.s.tech)
            self.rows.append(row)
            self.rows_box.addWidget(row)
            st = self.states.get(f.id, {}).get(p.key)
            if st:
                row.set_actual(st)
            elif not p.configured:
                row.set_actual(features.NEEDS_VALUES)
        self.confirm.setVisible(not f.verified)
        self.confirm.setChecked(False)
        self.render_hardware()
        self.update_pending()

    def set_tech(self, on: bool):
        self.s.tech = on
        self.show_feature()

    def scanned_identities(self) -> Dict[features.ModKey, dict]:
        return {(m.bus, m.addr): m.identity for m in self.s.modules.values()}

    def compat_html(self, f: features.Feature) -> str:
        if not self.s.modules or not any(p.part_number for p in f.parts):
            return ""
        res = features.compatibility(f, self.scanned_identities())
        level = features.compatibility_level(res)
        head = {features.SAME: (OK, "Your modules match the ones this was learned on"),
                features.FAMILY: (OK, "Same modules, different revision - very likely fine"),
                features.DIFFERENT: (DANGER, "Different modules from the ones this was learned on - the bits may "
                                             "mean something else on your car"),
                features.MISSING: (WARN, "A module this needs isn't on your car"),
                features.UNKNOWN_PART: (MUTED, "Couldn't compare part numbers")}[level]
        rows = "".join(f"<br>&nbsp;&nbsp;{p.label}: {detail}" for p, st, detail in res if st != features.SAME)
        return f"<br><span style='color:{head[0]}'>{head[1]}</span>{rows}"

    def mark_verified(self):
        f = self.feature()
        if not f:
            return
        if self.feature_state(f) != features.ENABLED:
            QMessageBox.information(self, "Hatchling", "Enable the feature and check it works on the car first.")
            return
        path = f.path
        if not path.startswith(paths.user_dir("features")):
            # built-in file: keep a verified copy in your own folder (it replaces the built-in one)
            import shutil
            path = os.path.join(paths.user_dir("features"), os.path.basename(f.path))
            shutil.copyfile(f.path, path)
        finder.set_verified(path, True)
        self.s.log(f"{f.name}: marked as working on this car")
        if f.library_id and library.repo():
            part = ""
            for p in f.parts:
                ident = self.s.modules.get(f"{p.bus}:{p.module:03X}")
                if ident and ident.identity.get("Part number"):
                    part = ident.identity["Part number"]
                    break
            if QMessageBox.question(
                    self, "Tell other owners?", f"Let others know <b>{f.name}</b> works on your car?<br><br>"
                    "GitHub opens with a confirmation ready - press <b>Submit new issue</b>. It shows your GitHub "
                    "username and your module's part number, never your VIN.") == QMessageBox.Yes:
                open_url(library.confirm_link(f.library_id, part))
        self.s.record(history.FEATURE, f"{f.name} - marked as working", {"feature": f.id})
        keep = f.id
        self.reload()
        self.select(keep)

    def select(self, fid: str):
        for i, x in enumerate(self.feats):
            if x.id == fid:
                self.list.setCurrentRow(i)
                return

    def share(self):
        f = self.feature()
        if not f:
            return
        if not f.verified:
            QMessageBox.information(self, "Hatchling", "Only features marked as working on your car can be shared.")
            return
        if not library.repo():
            QMessageBox.information(self, "Hatchling", "Set the library's GitHub repository first (Library page > "
                                                       "Library settings).")
            return
        with open(f.path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not any(m.get("part") for m in data.get("modules", [])):
            if QMessageBox.question(self, "Share", "This feature doesn't record which module part numbers it was "
                                    "learned on, so others can't check it matches their car. Share anyway?") \
                    != QMessageBox.Yes:
                return
        make = self.s.vin_info.make if self.s.vin_info else "Ford"
        url, text, prefilled = library.share_link(data, make)
        QApplication.clipboard().setText(text)
        mods = ", ".join(f"{p.label} ({p.bus} {p.module:03X})" for p in f.parts)
        if QMessageBox.question(
                self, "Share to library",
                f"Share <b>{f.name}</b>?<br><br>Modules: {mods}<br>Learned on: {f.learned_on or f.vehicle or 'not set'}"
                "<br><br>GitHub opens in your browser with the feature "
                + ("filled in" if prefilled else "form ready - paste the feature (it's on your clipboard) into "
                   "<b>Feature file</b>")
                + ". Choose the make and press <b>Submit new issue</b>. It appears in the library once it's "
                "reviewed.<br><br>Shared: the feature, the bits it changes and the module part numbers. "
                "<b>Not</b> shared: your VIN.") != QMessageBox.Yes:
            return
        open_url(url)
        self.s.log(f"Opened GitHub to share {f.name}")
        self.s.record(history.FEATURE, f"{f.name} shared to the library", {})

    def import_feature(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import a feature", os.path.expanduser("~"),
                                              "Hatchling feature (*.json)")
        if not path:
            return
        try:
            saved = finder.import_feature(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "Hatchling", f"That isn't a Hatchling feature file: {e}")
            return
        self.s.log(f"Feature imported: {saved}")
        with open(saved, encoding="utf-8") as fh:
            fid = json.load(fh).get("id", "")
        self.reload()
        self.select(fid)
        f = self.feature()
        msg = f"<b>{f.name}</b> added." if f else "Feature added."
        if f and self.s.modules:
            level = features.compatibility_level(features.compatibility(f, self.scanned_identities()))
            if level == features.DIFFERENT:
                msg += (f"<br><br><span style='color:{DANGER}'>Your modules are different from the ones this was "
                        "learned on.</span> Check the values match your car before enabling it.")
            elif level in (features.SAME, features.FAMILY):
                msg += "<br><br>Your modules match the ones it was learned on."
        msg += ("<br><br>It's marked <b>not tested on your car</b>. Enable it, check it works, then press "
                "<b>Mark as working on my car</b>.")
        if not self.s.modules:
            msg += "<br><br>Run a module scan to see whether it's already on and whether your modules match."
        QMessageBox.information(self, "Feature imported", msg)

    def export_feature(self):
        f = self.feature()
        if not f:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Export feature",
                                              os.path.join(os.path.expanduser("~"), os.path.basename(f.path)),
                                              "Hatchling feature (*.json)")
        if not dest:
            return
        finder.export_feature(f.path, dest)
        self.s.log(f"Feature exported: {dest}")
        QMessageBox.information(self, "Exported", f"Saved to {dest}<br><br>Send it to anyone with the same car - "
                                                  "they use <b>Import feature</b>, scan, and switch it on.")

    def render_hardware(self):
        f = self.feature()
        if not f:
            return
        if not f.checks:
            self.hw_list.setText(f"<span style='color:{MUTED}'>No hardware checks defined for this feature.</span>")
            self.hw_btn.setEnabled(False)
            return
        self.hw_btn.setEnabled(True)
        res = self.hw.get(f.id) or [None] * len(f.checks)
        lines = []
        for c, r in zip(f.checks, res):
            if r is None:
                icon, col, extra = "○", MUTED, "not checked"
            elif r[0]:
                icon, col, extra = "✓", OK, r[1]
            else:
                icon, col, extra = "✗", DANGER, r[1]
            lines.append(f"<span style='color:{col}'>{icon}</span>&nbsp; {c.label} "
                         f"<span style='color:{MUTED}'>- {extra}</span>")
        self.hw_list.setText("<br>".join(lines))

    def update_pending(self):
        f = self.feature()
        if not f:
            return
        for r in self.rows:
            r.refresh()
        usable = [r for r in self.rows if r.toggle.isEnabled()]
        self.master.setEnabled(bool(usable) and len(usable) == len(self.rows))
        self.master.setChecked(bool(usable) and all(r.toggle.isChecked() for r in usable))
        self.verify_btn.setVisible(not f.verified and self.feature_state(f) == features.ENABLED)
        self.share_btn.setVisible(f.verified and not f.library_id)
        n = sum(r.pending for r in self.rows)
        self.apply_btn.setText(f"Apply changes ({n})" if n else "Apply changes")
        self.apply_btn.setEnabled(n > 0)
        st = self.feature_state(f)
        hint = {"not read": "Scan modules, or press Read status, to see what's on now",
                features.NOT_FOUND: "This car doesn't have a module this feature needs",
                features.NEEDS_VALUES: "Add the block/byte/bit values to the feature file first"}.get(st, "")
        self.overall.setText(f"<span style='color:{STATE_COLORS.get(st, MUTED)}'>{st.capitalize()}</span>"
                             + (f" <span style='color:{MUTED}'>- {hint}</span>" if hint else ""))
        # banner when some modules are done and others aren't
        states = self.states.get(f.id, {})
        if st == features.MIXED:
            done = [p.label for p in f.parts if states.get(p.key) == features.ENABLED]
            todo = [p.label for p in features.remaining_parts(f, states)]
            self.banner_text.setText(f"<b>{f.name} is enabled in {len(done)} of {len(f.parts)} modules.</b><br>"
                                     f"Still needed: {', '.join(todo)}")
            self.banner.show()
        else:
            self.banner.hide()

    def master_clicked(self, checked: bool):
        for r in self.rows:
            if r.toggle.isEnabled():
                r.toggle.blockSignals(True)
                r.toggle.setChecked(checked)
                r.toggle.blockSignals(False)
        self.update_pending()

    # ---- adapter work -------------------------------------------------
    def _read_parts(self, parts: List[features.Part], then: Callable[[], None]):
        jobs = {}
        for bus in {p.bus for p in parts}:
            addrs = sorted({p.module for p in parts if p.bus == bus})

            def job(bus=bus, addrs=addrs):
                return {(bus, a): asbuilt.read_blocks(self.s.uds, self.s.module_for(bus, a)) for a in addrs}
            jobs[bus] = job
        self.s.per_bus(jobs, lambda b, r: self.blocks.update(r), then)

    def read_status(self, then: Optional[Callable[[], None]] = None):
        f = self.feature()
        if not f or not self.need_connection():
            return
        parts = [p for p in f.parts if p.configured]
        if not parts:
            QMessageBox.information(self, "Hatchling", "This feature has no values yet - edit its JSON file first.")
            return

        def done():
            self.states[f.id] = {p.key: features.part_state(p, self.blocks.get(p.key, {})) for p in parts}
            self.s.log(f"{f.name}: {self.feature_state(f)}")
            self.refresh_list()
            self.show_feature()
            if then:
                then()
        self._read_parts(parts, done)

    def check_hardware(self, then: Optional[Callable[[bool], None]] = None):
        """Runs manual checks (dialog) then module/bit checks on each bus. then(all_ok)."""
        f = self.feature()
        if not f or not self.need_connection():
            return
        if not f.checks:
            if then:
                then(True)
            return
        results: List[Optional[Tuple[bool, str]]] = [None] * len(f.checks)
        manual = [(i, c) for i, c in enumerate(f.checks) if c.kind == "manual"]
        if manual:
            dlg = ManualChecksDialog(self, f, [c for _, c in manual])
            accepted = bool(dlg.exec())
            for (i, _), a in zip(manual, dlg.answers()):
                results[i] = (accepted and a, "confirmed" if accepted and a else "not confirmed")
        auto = [(i, c) for i, c in enumerate(f.checks) if c.kind != "manual"]
        jobs = {}
        for bus in {c.bus for _, c in auto}:
            items = [(i, c) for i, c in auto if c.bus == bus]

            def job(items=items):
                return [(i, features.run_check(self.s.uds, c)) for i, c in items]
            jobs[bus] = job

        def each(bus, res):
            for i, r in res:
                results[i] = r

        def done():
            self.hw[f.id] = results
            ok = all(r and r[0] for r in results)
            self.s.log(f"{f.name}: hardware check {'passed' if ok else 'FAILED'}")
            self.render_hardware()
            if then:
                then(ok)
        if jobs:
            self.s.per_bus(jobs, each, done)
        else:
            done()

    def _require_hardware(self, f: features.Feature, proceed: Callable[[], None]):
        def after(ok: bool):
            if ok:
                proceed()
                return
            failed = [c.label for c, r in zip(f.checks, self.hw.get(f.id, [])) if not (r and r[0])]
            QMessageBox.warning(self, "Hardware missing",
                                f"<b>{f.name} can't be enabled yet.</b><br><br>These checks didn't pass:<br>• "
                                + "<br>• ".join(failed) + "<br><br>Fit the hardware, then try again.")
        if self.hardware_ok(f):
            proceed()
        else:
            self.check_hardware(after)

    def _write(self, f: features.Feature, wanted: Dict[features.ModKey, bool], ask: bool,
               then: Callable[[], None]):
        """Re-read, show the diff, back up, write, verify, then re-read status and call then()."""
        parts = [p for p in f.parts if p.key in wanted]

        def got_fresh():
            current = {k: self.blocks[k] for k in wanted}
            try:
                targets = features.plan(f, current, wanted)
            except KeyError as e:
                QMessageBox.warning(self, "Hatchling", f"A block this feature needs wasn't found on the module: {e}")
                return
            if not targets:
                self.read_status(then)
                return
            if ask:
                lines = []
                for p in parts:
                    if p.key in targets:
                        lines.append(f"<b>{p.label}</b> → {'ON' if wanted[p.key] else 'OFF'}")
                        for d, i, a, b in asbuilt.diff(current[p.key], targets[p.key]):
                            lines.append(f"&nbsp;&nbsp;{d:04X} byte {i}: {a:02X} → {b:02X}")
                if QMessageBox.question(self, f.name, "<br>".join(lines) +
                                        "<br><br>Each module is backed up before writing. Keep the ignition on "
                                        "and a charger connected.") != QMessageBox.Yes:
                    return
            jobs = {}
            for bus in {k[0] for k in targets}:
                keys = [k for k in targets if k[0] == bus]

                def job(keys=keys):
                    return [(k, asbuilt.safe_write(self.s.uds, self.s.module_for(*k), current[k], targets[k],
                                                   self.s.vin, self.s.log)) for k in keys]
                jobs[bus] = job
            failures = []

            def each(bus, res):
                for k, r in res:
                    label = next(p.label for p in parts if p.key == k)
                    self.s.record(history.FEATURE, f"{f.name} - {label} {'ON' if wanted[k] else 'OFF'}"
                                  + ("" if r.verified else " (verify failed)"),
                                  {"feature": f.id, "module": f"{k[1]:03X}", "bus": k[0], "on": wanted[k],
                                   "verified": r.verified, "backup": r.backup_path})
                    if not r.verified:
                        failures.append(f"{label}: {r.message}")

            def done():
                if failures:
                    QMessageBox.critical(self, f.name, "<br>".join(failures))
                self.read_status(then)
            self.s.per_bus(jobs, each, done)
        self._read_parts(parts, got_fresh)

    def apply_changes(self):
        f = self.feature()
        if not f or not self.need_connection():
            return
        if not f.verified and not self.confirm.isChecked():
            QMessageBox.warning(self, "Hatchling", "This feature isn't marked verified. Tick the confirmation box "
                                                 "once you've checked its values for your car.")
            return
        wanted = {r.part.key: r.toggle.isChecked() for r in self.rows if r.pending}
        if not wanted:
            return
        enabling = any(wanted.values())
        go = lambda: self._write(f, wanted, True, lambda: self.after_write(f, enabling))
        if enabling:
            self._require_hardware(f, go)
        else:
            go()

    def after_write(self, f: features.Feature, enabling: bool):
        st = self.feature_state(f)
        if st == features.ENABLED and enabling:
            self.celebrate(f)
        elif st == features.DISABLED and not enabling:
            QMessageBox.information(self, f.name, f"{f.name} is now disabled.<br><br>"
                                                  "Turn the ignition off for 30 seconds, then back on.")
        elif st == features.MIXED and enabling:
            todo = [p.label for p in features.remaining_parts(f, self.states.get(f.id, {}))]
            r = QMessageBox.question(self, f.name,
                                     f"Done. {f.name} still needs enabling in:<br>• " + "<br>• ".join(todo) +
                                     "<br><br>Enable it in the other modules now?")
            if r == QMessageBox.Yes:
                self.start_guide()

    def celebrate(self, f: features.Feature):
        self.s.log(f"{f.name} is now enabled in all {len(f.parts)} module(s)")
        SuccessDialog(self, f"{f.name} is now enabled",
                      [f"Written and verified in all {len(f.parts)} module(s).",
                       "Turn the ignition off for 30 seconds, then back on to use it."]
                      + ([] if f.verified else ["Once you've checked it works, press "
                                                "<b>Mark as working on my car</b>."])).exec()

    # ---- guided walkthrough ---------------------------------------------
    def start_guide(self):
        f = self.feature()
        if not f or not self.need_connection():
            return
        if not f.verified and not self.confirm.isChecked():
            QMessageBox.warning(self, "Hatchling", "Tick the confirmation box first - this feature isn't verified.")
            return
        todo = features.remaining_parts(f, self.states.get(f.id, {}))
        if not todo:
            self.celebrate(f)
            return
        self._require_hardware(f, lambda: self._guide_step(f, todo, 1, len(todo)))

    def _guide_step(self, f: features.Feature, queue: List[features.Part], step: int, total: int):
        if not queue:
            self.read_status(lambda: self._guide_finish(f))
            return
        part, rest = queue[0], queue[1:]
        dlg = GuideDialog(self, f, part, step, total, self.s.elm.bus if self.s.elm else None)
        if not dlg.exec():
            return
        if dlg.choice == GuideDialog.SKIP:
            self._guide_step(f, rest, step + 1, total)
        elif dlg.choice == GuideDialog.OPEN:
            ch = part.changes[0] if part.changes else None
            self.s.open_config(part.bus, part.module, ch.did if ch else None, ch.byte if ch else None)
        elif dlg.choice == GuideDialog.ENABLE:
            self._write(f, {part.key: True}, False, lambda: self._guide_step(f, rest, step + 1, total))

    def _guide_finish(self, f: features.Feature):
        st = self.feature_state(f)
        if st == features.ENABLED:
            self.celebrate(f)
        else:
            todo = [p.label for p in features.remaining_parts(f, self.states.get(f.id, {}))]
            QMessageBox.information(self, f.name, f"{f.name} is enabled in some modules.<br>Still needed:<br>• "
                                    + "<br>• ".join(todo))


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Hatchling {VERSION}")
        self.resize(1180, 760)
        self.s = Session()
        self.s.window = self
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        self.stack = QStackedWidget()
        # (key, label in the sidebar, page class) - keys are what the code refers to
        groups = [
            (None, [("Home", "Home", HomePage)]),
            ("Workshop", [("Fault codes", "Fault codes", DtcPage), ("Live data", "Live data", LivePage),
                          ("Dashboard", "Gauges", DashboardPage), ("Features", "Features", FeaturesPage),
                          ("Library", "Feature library", LibraryPage), ("Modules", "Modules", ModulesPage),
                          ("Service", "Service jobs", ServicePage), ("Configuration", "Module settings", AsBuiltPage),
                          ("History", "History", HistoryPage)]),
            ("Setup", [("Connect", "Connection", ConnectPage)]),
        ]
        brand = QListWidgetItem(QIcon(paths.asset("logo.png")), "  Hatchling")
        brand.setFlags(Qt.NoItemFlags)
        f = brand.font()
        f.setFamily(theme.HEAD_FONT)
        f.setPointSize(14)
        brand.setFont(f)
        brand.setForeground(QColor("#FFFFFF"))
        self.nav.setIconSize(QSize(26, 26))
        self.nav.addItem(brand)
        self.pages = {}
        self.rows = {}
        for group, items in groups:
            if group:   # Home sits on its own at the top, no heading
                head = QListWidgetItem(group)
                head.setFlags(Qt.NoItemFlags)
                hf = head.font()
                hf.setPointSize(9)
                head.setFont(hf)
                self.nav.addItem(head)
            for key, label, cls in items:
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, key)
                self.nav.addItem(it)
                self.rows[key] = self.nav.count() - 1
                self.pages[key] = cls(self.s)
                self.stack.addWidget(self.pages[key])
        self.nav.currentItemChanged.connect(self.on_nav)
        self.s.open_config = self.open_config
        self.s.focus_mode = self.focus_mode
        home = self.pages["Home"]
        home.go = self.go
        home.go_codes = lambda: self.pages["Fault codes"].read_all()
        self.go("Home")
        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.logbox = QPlainTextEdit()
        self.logbox.setObjectName("log")
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(2000)
        self.logbox.setFixedHeight(130)
        self.logbox.hide()
        outer.addWidget(self.logbox)
        self.setCentralWidget(central)

        self.conn = QLabel("● Not connected")
        self.bus = QLabel("")
        self.busy = QLabel("")
        self.log_btn = button("Show activity")
        self.log_btn.setObjectName("link")
        self.log_btn.clicked.connect(self.toggle_log)
        self.night_btn = button("Night mode" if not library.settings().get("dark_mode") else "Day mode")
        self.night_btn.setObjectName("link")
        self.night_btn.clicked.connect(self.toggle_night)
        sb = self.statusBar()
        sb.addWidget(self.conn)
        sb.addWidget(self.bus)
        sb.addPermanentWidget(self.busy)
        sb.addPermanentWidget(self.log_btn)
        sb.addPermanentWidget(self.night_btn)
        self.s.logged.connect(self.logbox.appendPlainText)
        self.s.connected_changed.connect(self.on_conn)
        self.s.bus_changed.connect(lambda b: self.bus.setText(
            f"Adapter switch: {BUSES[b].switch} ({BUSES[b].label})" if b in BUSES else ""))
        self.s.runner.busy_changed.connect(lambda b: self.busy.setText("Talking to the car…" if b else ""))
        self.s.log("Hatchling started. Backups go to " + asbuilt.BACKUP_DIR)
        QTimer.singleShot(3000, self.check_updates)

    def go(self, key: str):
        self.nav.setCurrentRow(self.rows[key])

    def on_nav(self, cur, _prev):
        key = cur.data(Qt.UserRole) if cur else None
        if key in self.pages:
            self.stack.setCurrentWidget(self.pages[key])

    def toggle_log(self):
        show = not self.logbox.isVisible()
        self.logbox.setVisible(show)
        self.log_btn.setText("Hide activity" if show else "Show activity")

    def toggle_night(self):
        cfg = library.settings()
        cfg["dark_mode"] = not cfg.get("dark_mode", False)
        library.save_settings(cfg)
        self.night_btn.setText("Day mode" if cfg["dark_mode"] else "Night mode")
        QMessageBox.information(self, "Hatchling", ("Night" if cfg["dark_mode"] else "Day") +
                                " mode is set. Close and reopen Hatchling to switch.")

    def focus_mode(self, on: bool):
        """Full-screen with only the current page (used by the dashboard)."""
        for w in (self.nav, self.statusBar()):
            w.setVisible(not on)
        if on:
            self.logbox.hide()
        if on:
            self._was_max = self.isMaximized()
            self.showFullScreen()
        elif getattr(self, "_was_max", False):
            self.showMaximized()
        else:
            self.showNormal()

    def check_updates(self):
        def ok(rel):
            if rel is None:
                return
            self.s.log(f"Update available: Hatchling {rel.version}")
            b = button(f"Update available: v{rel.version} - download", "primary")
            b.clicked.connect(lambda: __import__("webbrowser").open(rel.url))
            self.statusBar().addPermanentWidget(b)
        self.s.runner.run(updates.check, ok, lambda e: self.s.log(f"Update check skipped: {e}"))

    def open_config(self, bus: str, addr: int, did: Optional[int] = None, byte: Optional[int] = None):
        self.go("Configuration")
        self.pages["Configuration"].open_module(bus, addr, did, byte)

    def on_conn(self, ok):
        self.conn.setText(f"<span style='color:{OK if ok else MUTED}'>●</span> "
                          f"{'Connected' + (' to the simulator' if self.s.simulated else '') if ok else 'Not connected'}")
        if not ok:
            self.bus.setText("")

    def closeEvent(self, e):
        if self.s.elm:
            self.s.elm.close()
        self.s.runner.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(e)


def excepthook(t, v, tb):
    traceback.print_exception(t, v, tb)
    if Session.instance:
        Session.instance.log("UNEXPECTED: " + "".join(traceback.format_exception_only(t, v)).strip())


def main():
    sys.excepthook = excepthook
    app = QApplication(sys.argv)
    app.setApplicationName("Hatchling")
    app.setStyleSheet(theme.STYLE)
    icon = QIcon(paths.asset("hatchling.ico"))
    app.setWindowIcon(icon)
    from ui.splash import Splash
    holder = {}

    def build():
        splash.set_status("Preparing your Hatchling folder")
        paths.ensure_user_dirs()
        splash.set_status("Loading vehicles and features")
        holder["w"] = MainWindow()

    def done():
        w = holder.get("w") or MainWindow()
        w.show()
        w.raise_()
        w.activateWindow()
        splash.deleteLater()

    splash = Splash(VERSION, done)
    splash.start()
    QTimer.singleShot(60, build)
    sys.exit(app.exec())
