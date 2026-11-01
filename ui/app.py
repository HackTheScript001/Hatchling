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

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from core import (asbuilt, dtc, factory, features, finder, history, library, online, livedata, modules, paths, report,
                  safety, services, updates, vehicles, vin)
from core.version import VERSION
from core.buses import BUSES, OBD
from core.elm import (DRIVER_PAGES, ELM327, HS, MS, WIFI_DEFAULT, ElmError, describe_ports, find_baud, open_transport,
                      parse_wifi)
from core.obd import GenericObd
from core.modules import Module
from core.pids import PIDS
from core.sim import SimTransport
from core.uds import UDS, UdsError
from ui import theme
from ui.theme import ACCENT, AMBER, CHANGED, DANGER, FAINT, INK, LINE, LINE_STRONG, MUTED, OK, PANEL_ALT, WARN

SIM = "Simulator - 2015 Fiesta (CAN)"
SIM_OLD = "Simulator - 2003 Focus (OBD only)"
WIFI = "Wi-Fi adapter"
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
    library_changed = Signal()
    tech_changed = Signal(bool)   # {"states": {...}, "blocks": {...}} after a module scan

    def __init__(self):
        super().__init__()
        Session.instance = self
        self.runner = Runner()   # the adapter - one job at a time
        self.net = Runner()      # downloads and other internet jobs, so they never hold up the car
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
        self.port, self.baud = "", 38400
        self.paused = False
        self.voltage: Optional[float] = None
        self.tech = False          # show block/byte/bit details
        self.window: Optional[QWidget] = None
        self.open_config: Callable = lambda *a: None  # set by MainWindow
        self.focus_mode: Callable[[bool], None] = lambda on: None  # set by MainWindow
        self.go: Callable[[str], None] = lambda key: None              # set by MainWindow
        self.open_learn: Callable[..., None] = lambda name="": None    # set by MainWindow
        self.open_factory_diff: Callable[[], None] = lambda: None      # set by MainWindow
        self.full_backup_done: set = set()   # VINs backed up in full during this visit
        self.feature_states: dict = {}       # from the last module scan
        self.features_detected.connect(lambda d: setattr(self, "feature_states", d.get("states", {})))
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

    def pause_adapter(self):
        """Closes the COM port so another program can use the adapter (keeps everything else)."""
        if self.elm and not self.simulated and not self.paused:
            self.elm.close()
            self.paused = True
            self.log("Adapter freed for another program")

    def resume_adapter(self):
        """Reopens the COM port after pause_adapter (runs on the worker thread)."""
        if not self.paused or not self.elm:
            return
        self.elm.t = open_transport(self.port, self.baud)
        self.elm.bus = None
        self.elm._target = None
        self.elm.initialise()
        self.paused = False
        self.log("Adapter reconnected")

    # ---- safety before any change to the car ------------------------------------
    def before_write(self, then: Callable[[], None]):
        """Battery check, then a full backup of every module (first change of the visit only), then then()."""
        if not self.connected:
            return

        def checked(volts):
            self.voltage = volts if volts is not None else self.voltage
            state, msg = safety.battery_state(volts)
            self.log(f"Battery check before writing: {f'{volts:.1f} V' if volts else 'not read'} ({state})")
            if state == safety.TOO_LOW:
                QMessageBox.critical(self.window, "Battery too low", msg)
                return
            if state in (safety.LOW, safety.UNKNOWN):
                box = QMessageBox(QMessageBox.Warning, "Check the battery", msg, parent=self.window)
                go = box.addButton("Continue anyway", QMessageBox.AcceptRole)
                box.addButton("Stop - I'll connect a charger", QMessageBox.RejectRole)
                box.exec()
                if box.clickedButton() is not go:
                    return
            if self.vin in self.full_backup_done or not self.modules:
                then()
            else:
                self.full_backup(lambda path: then())
        self.runner.run(lambda: self.elm.voltage(), checked, lambda e: checked(None))

    def full_backup(self, then: Callable[[str], None] = lambda path: None, note: str = "automatic, before the "
                                                                                       "first change of the visit"):
        """Reads every scanned module's settings into one backup file."""
        mods = [m for m in self.modules.values() if m.bus in BUSES and BUSES[m.bus].protocol == "can"]
        seen, chosen = set(), []
        for m in sorted(mods, key=lambda m: m.bus != (self.elm.bus if self.elm else "")):
            if m.addr not in seen:     # a module on both buses only needs backing up once
                seen.add(m.addr)
                chosen.append(m)
        buses = {m.bus for m in chosen}
        if not chosen:
            then("")
            return
        needs_flip = not (self.simulated or self.auto_switch) and len({BUSES[b].switch for b in buses}) > 1
        self.log(f"Backing up all {len(chosen)} module(s) before the first change"
                 + (" - you'll be asked to flip the adapter switch" if needs_flip else ""))
        got: List[Tuple[Module, asbuilt.Blocks]] = []
        jobs = {b: (lambda b=b: safety.read_modules(self.uds, [m for m in chosen if m.bus == b], self.log))
                for b in buses}

        def done():
            path = safety.save_full(self.vin, got, note)
            self.full_backup_done.add(self.vin)
            self.log(f"Full backup saved ({len(got)} of {len(chosen)} module(s)): {path}")
            self.record(history.NOTE, f"Full backup of {len(got)} module(s)", {"backup": path})
            then(path)
        self.per_bus(jobs, lambda b, r: got.extend(r), done)

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
        self.vin_card = QLabel("")
        self.vin_card.setWordWrap(True)
        self.vin_card.setTextFormat(Qt.RichText)
        self.vin_card.setObjectName("subtle")
        self.root.addWidget(self.vin_card)
        self.vin_details: Dict[str, str] = {}
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
        s.connected_changed.connect(self.on_connected)
        s.modules_changed.connect(self.refresh)
        s.factory_changed.connect(self.refresh)
        s.codes_read.connect(lambda n: (self.counts.__setitem__("codes", n), self.refresh()))
        s.features_detected.connect(self.on_features)
        self.refresh()

    def on_connected(self, ok: bool):
        self.vin_details = {}
        self.refresh()
        if ok and self.s.vin and not self.s.simulated:
            year = self.s.vin_info.year if self.s.vin_info else None
            vin_txt = self.s.vin

            def got(d):
                if self.s.vin == vin_txt:
                    self.vin_details = d
                    self.refresh()
            self.s.net.run(lambda: online.vin_details(vin_txt, year), got,
                              lambda e: self.s.log(f"VIN details not available: {e}"))

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
            d = self.vin_details
            make = (d.get("Make") or (info.make if info else "")).title() if (d.get("Make") or info) else ""
            model = d.get("Model", "")
            year = d.get("Model year") or (str(info.year) if info and info.year else "")
            name = " ".join(x for x in (make, model) if x) or "Connected"
            self.carname.setText(f"{name}, {year}" if year else name)
            bits = [f"VIN {s.vin}" if s.vin else "VIN not read"]
            if s.profile:
                bits.append(s.profile.name)
            self.carline.setText(". ".join(bits) + ("." if bits else ""))
            self.connect_btn.hide()
        else:
            self.carname.setText("No car connected")
            self.carline.setText("Plug the adapter into the port under the dashboard and switch the ignition on.")
            self.connect_btn.show()
        d = self.vin_details
        if s.connected and d:
            keep = [k for k in ("Body", "Engine size (L)", "Engine", "Fuel", "Gearbox", "Trim", "Built in",
                                "Country built") if d.get(k)]
            parts = []
            for k in keep:
                v = html.escape(d[k])
                parts.append(f"{k}: <b>{v}{' L' if k == 'Engine size (L)' else ''}</b>".replace("Engine size (L)",
                                                                                              "Engine size"))
            self.vin_card.setText(" &nbsp; ".join(parts) + "<br><span style='font-size:9pt'>VIN details from the "
                                  "NHTSA VIN database - fullest for North American cars.</span>" if parts else "")
        else:
            self.vin_card.setText("")
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
        self.baud.addItems(["Automatic", "38400", "115200", "57600", "500000"])
        self.baud.setToolTip("How fast the laptop talks to the adapter. Automatic tries each speed and remembers "
                             "the one that works.")
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
        self.port.addItem("Wi-Fi adapter (join its Wi-Fi network first)", WIFI)
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
        if port == WIFI:
            from PySide6.QtWidgets import QInputDialog
            cfg = library.settings()
            addr, ok = QInputDialog.getText(self, "Wi-Fi adapter", "Adapter address (most use 192.168.0.10:35000):",
                                            text=cfg.get("wifi_adapter", WIFI_DEFAULT))
            if not ok:
                return
            try:
                parse_wifi(addr)
            except ElmError as e:
                QMessageBox.warning(self, "Hatchling", str(e))
                return
            cfg["wifi_adapter"] = addr.strip()
            library.save_settings(cfg)
            port = "tcp://" + addr.strip().removeprefix("tcp://")
        sim = port in (SIM, SIM_OLD)
        legacy = port == SIM_OLD
        baud_choice = self.baud.currentText()
        remembered = library.settings().get("baud_by_port", {}).get(port)
        trace = self.trace.isChecked()
        chosen = self.selected_profile()
        self.btn.setEnabled(False)
        self.info.setText("Connecting...")

        def connect():
            if sim or port.startswith("tcp://"):
                baud = 38400   # not used for Wi-Fi
            elif baud_choice == "Automatic":
                baud = find_baud(port, remembered, log=self.s.log)
            else:
                baud = int(baud_choice)
            t = SimTransport(legacy=legacy) if sim else open_transport(port, baud)
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
            return elm, uds, obd, v, vin_txt, has_can, proto, baud

        def ok(res):
            elm, uds, obd, v, vin_txt, has_can, proto, baud = res
            if not sim and not port.startswith("tcp://"):
                cfg = library.settings()
                cfg.setdefault("baud_by_port", {})[port] = baud
                library.save_settings(cfg)
            s = self.s
            s.elm, s.uds, s.obd = elm, uds, obd
            s.simulated = sim
            s.port, s.baud, s.paused = port, baud, False
            s.auto_switch = self.auto.isChecked()
            s.vin = vin_txt
            s.vin_info = vin.decode(vin_txt)
            s.voltage = v
            s.factory = factory.load(vin_txt) if vin_txt else None
            if sim and not legacy and not s.factory:
                from core.sim import sample_factory
                s.factory = sample_factory()
                s.log("Simulator: sample factory file loaded (auto headlights were off when it was built)")
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
        self.get_btn = button("Get my factory file", "primary")
        self.get_btn.setToolTip("Opens Ford's As-Built page with your VIN copied, then imports the download "
                                "automatically")
        self.get_btn.clicked.connect(self.get_factory_file)
        self.import_btn = button("Import factory file")
        self.import_btn.clicked.connect(lambda: import_factory_file(self, self.s))
        self.watch_timer = QTimer(self)
        self.watch_timer.setInterval(2000)
        self.watch_timer.timeout.connect(self.check_downloads)
        self.watch_since = 0.0
        self.watch_tried: set = set()
        self.diff_btn = button("Changes since factory")
        self.diff_btn.clicked.connect(self.changes_since_factory)
        self.donor_btn = button("Compare with donor car")
        self.donor_btn.clicked.connect(self.compare_donor)
        self.share_fac_btn = button("Share my factory file")
        self.share_fac_btn.clicked.connect(lambda: ShareFactoryDialog(self, self.s).exec())
        frow.addWidget(self.factory_label, 1)
        frow.addWidget(self.get_btn)
        frow.addWidget(self.import_btn)
        frow.addWidget(self.diff_btn)
        frow.addWidget(self.donor_btn)
        frow.addWidget(self.share_fac_btn)
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

    def get_factory_file(self):
        """Copies the VIN, opens Ford's As-Built page and watches Downloads for the file."""
        if not self.s.connected or not self.s.vin:
            QMessageBox.information(self, "Hatchling", "Connect to the car first so Hatchling can read its VIN.")
            return
        QApplication.clipboard().setText(self.s.vin)
        url = library.settings().get("factory_url") or online.FACTORY_URL
        QMessageBox.information(
            self, "Get my factory file",
            f"Your VIN <b>{self.s.vin}</b> is copied.<br><br>"
            "1. Ford's As-Built page opens in your browser - sign in with your own free account (or create one).<br>"
            "2. Paste the VIN (Ctrl+V) and download the As-Built file.<br><br>"
            "Leave Hatchling open - it spots the file in your Downloads folder and imports it automatically.")
        open_url(url)
        self.watch_since = time.time()
        self.watch_tried = set()
        self.watch_started = time.time()
        self.watch_timer.start()
        self.factory_label.setText(f"<span style='color:{WARN}'>●</span> Waiting for the factory file in "
                                   f"{online.downloads_dir()} ...")
        self.s.log("Watching Downloads for the factory file")

    def check_downloads(self):
        if time.time() - self.watch_started > 15 * 60:
            self.watch_timer.stop()
            self.s.log("Stopped watching Downloads (15 minutes)")
            self.show_factory()
            return
        for path in online.new_factory_files(online.downloads_dir(), self.watch_since):
            if path in self.watch_tried:
                continue
            self.watch_tried.add(path)
            try:
                data = factory.load_file(path)
            except (OSError, factory.FactoryError, ValueError):
                continue        # not an As-Built file (or still downloading) - keep watching
            if data.vin and self.s.vin and data.vin != self.s.vin:
                continue        # someone else's file
            self.watch_timer.stop()
            self.s.log(f"Found factory file: {path}")
            import_factory_path(self, self.s, path)
            return

    def show_factory(self):
        f = self.s.factory
        if f:
            self.factory_label.setText(f"<span style='color:{OK}'>●</span> Factory As-Built loaded for "
                                       f"{f.vin} ({len(f.modules)} modules, imported {f.imported})")
            self.diff_btn.setEnabled(True)
            self.donor_btn.setEnabled(True)
            self.share_fac_btn.setEnabled(True)
        else:
            others = [v for v, _ in factory.stored() if v != self.s.vin]
            if others and self.s.connected and self.s.vin:
                where = "the simulator" if self.s.simulated else "this car"
                self.factory_label.setText(
                    f"<span style='color:{WARN}'>●</span> You have a factory file for <b>{', '.join(others[:3])}</b>, "
                    f"but you're connected to {where} ({self.s.vin}). Connect to "
                    f"{'your car' if self.s.simulated else 'that car'} to use it.")
            elif others and not self.s.connected:
                self.factory_label.setText(
                    f"<span style='color:{OK}'>●</span> Factory file saved for <b>{', '.join(others[:3])}</b>. "
                    "Connect to the car and it's used automatically.")
            else:
                self.factory_label.setText(f"<span style='color:{WARN}'>●</span> No factory As-Built yet. Before "
                                           "changing anything, use <b>Get my factory file</b> to download it for "
                                           "your VIN and import it here.")
            self.diff_btn.setEnabled(False)
            self.donor_btn.setEnabled(False)
            self.share_fac_btn.setEnabled(False)

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
        """One row per module - a gateway module that answers on both buses (like the body module) is
        shown once, with both buses."""
        self.tbl.setRowCount(0)
        rows: Dict[tuple, list] = {}
        for m in sorted(self.s.modules.values(), key=lambda m: (m.bus, m.addr)):
            key = (m.addr, m.identity.get("Part number", "") or m.key, m.identity.get("Software", ""))
            rows.setdefault(key, []).append(m)
        for ms in sorted(rows.values(), key=lambda ms: (ms[0].bus, ms[0].addr)):
            m = ms[0]
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            vals = [f"{m.addr:03X}", " + ".join(x.bus for x in ms), m.name, m.identity.get("Part number", ""),
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
        self.mot = button("MOT check")
        self.mot.setToolTip("Checks the engine's emissions self-tests - the ones an MOT tester looks at")
        self.read.clicked.connect(self.read_all)
        self.details.clicked.connect(self.show_details)
        self.clear.clicked.connect(self.clear_all)
        self.export.clicked.connect(self.export_report)
        self.mot.clicked.connect(self.mot_check)
        for w in (self.read, self.details, self.clear, self.mot):
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
        if r < 0 or r >= len(self.found) or not self.need_connection():
            return
        m, raw, st = self.found[r]
        code = self.tbl.item(r, 1).text()
        if code == "-":
            return
        if not raw:   # generic OBD-II code: the engine's freeze frame
            def ok(frame):
                CodeDialog(self, self.s, code, "Engine", self.tbl.item(r, 2).text(),
                           [("Freeze frame", "<br>".join(f"{html.escape(k)}: <b>{html.escape(str(v))}</b>"
                                                         for k, v in frame.items()) or "none stored")]).exec()
            self.s.ensure_bus(OBD, lambda: self.s.runner.run(self.s.obd.freeze_frame, ok,
                                                             lambda e: CodeDialog(self, self.s, code, "Engine", "",
                                                                                  [("Freeze frame", str(e))]).exec()))
            return

        def job():
            out = {}
            for name, fn in (("Freeze frame", self.s.uds.dtc_snapshot), ("Extended data", self.s.uds.dtc_extended)):
                try:
                    out[name] = fn(m.addr, raw)
                except UdsError as e:
                    out[name] = str(e)
            return out

        def ok(res):
            sections = []
            for name, v in res.items():
                if isinstance(v, (bytes, bytearray)):
                    body = decode_snapshot(v) if name == "Freeze frame" else v.hex(" ").upper() or "none"
                else:
                    body = f"<span style='color:{MUTED}'>{html.escape(v)}</span>"
                sections.append((name, body))
            CodeDialog(self, self.s, code, m.name, f"{dtc.describe_status(st)} (0x{st:02X})", sections).exec()
        self.s.ensure_bus(m.bus, lambda: self.s.runner.run(job, ok))

    def mot_check(self):
        """The engine's emissions self-tests (readiness monitors), as an MOT emissions test reads them."""
        if not self.need_connection():
            return
        if self.s.obd_only:
            bus, get = OBD, self.s.obd.pid
        else:
            bus, get = HS, (lambda pid: self.s.uds.obd_pid(pid, 0x7E0))

        def ok(r):
            MotDialog(self, r).exec()
            self.s.record(history.NOTE, "MOT check: " + ("looks ready" if r["ready"] else
                                                        f"not ready ({', '.join(r['incomplete']) or 'warning light on'})"),
                          {"incomplete": r["incomplete"], "mil": r["mil"]})
        self.s.ensure_bus(bus, lambda: self.s.runner.run(lambda: obd_readiness(get), ok))

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
        """A car health report - fault codes, MOT readiness, features and modules - saved as a PDF."""
        if not self.s.modules:
            QMessageBox.information(self, "Hatchling", "Scan the car first (and read the fault codes) to build a "
                                                       "report.")
            return
        rows = [tuple(self.tbl.item(r, c).text() for c in range(4)) for r in range(self.tbl.rowCount())
                if self.tbl.item(r, 1).text() != "-"]
        default = os.path.join(report.REPORT_DIR, time.strftime(f"Car health report {self.s.vin or ''} %Y-%m-%d.pdf"))
        os.makedirs(report.REPORT_DIR, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "Save car health report", default, "PDF (*.pdf)")
        if not path:
            return
        mods = list({m.addr: m for m in self.s.modules.values()}.values())
        feats = [f for f in features.load_all() if f.configured]
        groups = features.summary(feats, self.s.feature_states) if self.s.feature_states else None

        def build(readiness):
            doc_html = report.build_html(self.s.vin, self.s.voltage, mods, rows,
                                         self.s.vin_info.summary if self.s.vin_info else "", readiness, groups, True)
            try:
                save_pdf(doc_html, path)
            except OSError as e:
                QMessageBox.warning(self, "Hatchling", f"Couldn't save the report: {e}")
                return
            self.s.log(f"Report saved: {path}")
            open_url("file:///" + path.replace(os.sep, "/"))
        if not self.s.connected:
            build(None)
            return
        bus, get = (OBD, self.s.obd.pid) if self.s.obd_only else (HS, lambda pid: self.s.uds.obd_pid(pid, 0x7E0))
        self.s.ensure_bus(bus, lambda: self.s.runner.run(lambda: obd_readiness(get), build, lambda e: build(None)))


def save_pdf(doc_html: str, path: str):
    from PySide6.QtGui import QPageSize, QPdfWriter, QTextDocument
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setResolution(110)
    writer.setTitle("Car health report")
    doc = QTextDocument()
    doc.setDefaultStyleSheet("body{font-family:'Segoe UI',Arial;font-size:10pt;color:#1b2430}"
                             "h1{font-size:18pt} h2{font-size:13pt;margin-top:14px} th{background:#eef2f6}"
                             "td,th{border-color:#ccd5df}")
    doc.setHtml(doc_html)
    doc.setPageSize(QSizeF(writer.width(), writer.height()))
    doc.print_(writer)
    if not os.path.isfile(path):
        raise OSError("the PDF wasn't written")


def obd_readiness(get):
    from core.obd import readiness_report
    return readiness_report(get)


class CodeDialog(QDialog):
    """One fault code: what it means, the conditions when it was stored, and what fixed it for others."""

    def __init__(self, parent, s: "Session", code: str, module: str, status: str, sections: List[Tuple[str, str]]):
        super().__init__(parent)
        self.s, self.code = s, code
        self.setWindowTitle(code)
        self.setMinimumSize(560, 460)
        lay = QVBoxLayout(self)
        base = code.split("-")[0]
        meaning = dtc.description(code)
        parts = [f"<h2 style='margin:0'>{html.escape(code)}</h2>",
                 f"<p style='color:{MUTED}'>{html.escape(module)}{' · ' + html.escape(status) if status else ''}</p>",
                 f"<p><b>{html.escape(meaning) if meaning else 'No description for this code yet.'}</b></p>"]
        if base[:2] in ("P1", "P3", "B1", "B2", "C1", "U1", "U2") and not meaning:
            parts.append(f"<p style='color:{MUTED}'>Codes like this are set by Ford rather than the common "
                         "standard, so the meaning depends on the module.</p>")
        for name, body in sections:
            parts.append(f"<h3>{html.escape(name)}</h3><p>{body}</p>")
        tips = library.code_tips(base)
        parts.append("<h3>What fixed it for other owners</h3>")
        if tips:
            parts.append("".join(f"<p>• {html.escape(t['text'])}"
                                 f"{' <span style=color:' + MUTED + '>(' + html.escape(t['car']) + ')</span>' if t.get('car') else ''}"
                                 "</p>" for t in tips))
        else:
            parts.append(f"<p style='color:{MUTED}'>No tips for {html.escape(base)} yet. If you fix it, share what "
                         "worked so the next person knows where to look.</p>")
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setHtml("".join(parts))
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        search = button("Search the community")
        search.clicked.connect(lambda: self._open(lambda: library.code_search_link(base)))
        share = button("Share what fixed it")
        share.clicked.connect(lambda: self._open(lambda: library.code_fix_link(
            base, s.vin_info.summary if s.vin_info else "")))
        close = button("Close", "primary")
        close.clicked.connect(self.accept)
        row.addWidget(search)
        row.addWidget(share)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)

    def _open(self, url_fn):
        try:
            open_url(url_fn())
        except library.LibraryError as e:
            QMessageBox.information(self, "Hatchling", str(e))


class MotDialog(QDialog):
    def __init__(self, parent, r: dict):
        super().__init__(parent)
        self.setWindowTitle("MOT check")
        self.setMinimumSize(520, 520)
        lay = QVBoxLayout(self)
        if r["ready"]:
            head = f"<h2 style='color:{OK};margin:0'>Looks ready for the emissions check</h2>"
        else:
            head = f"<h2 style='color:{WARN};margin:0'>Not ready yet</h2>"
        why = []
        if r["mil"]:
            why.append(f"The engine warning light is <b>on</b> ({r['codes']} code(s)) - that's an MOT fail on its own. "
                       "Read the fault codes and fix the cause first.")
        if r["incomplete"]:
            why.append("These self-tests haven't finished: <b>" + ", ".join(r["incomplete"]) + "</b>. They finish "
                       "by themselves with normal driving - usually a mix of town and steady 40-60 mph driving over a "
                       "few days. Clearing codes or disconnecting the battery resets them.")
        if not why:
            why.append("No warning light and the self-tests have finished.")
        since = []
        if "warmups" in r:
            since.append(f"{r['warmups']} warm-up(s)")
        if "distance_km" in r:
            since.append(f"{r['distance_km']} km ({round(r['distance_km'] * 0.621)} miles)")
        info = QLabel(head + "<p>" + "</p><p>".join(why) + "</p>" +
                      (f"<p style='color:{MUTED}'>Since the codes were last cleared: {', '.join(since)}.</p>" if since else "") +
                      f"<p style='color:{MUTED}'>A guide only - the tester's own equipment and rules decide. "
                      f"{'Diesel' if r['diesel'] else 'Petrol'} engine.</p>")
        info.setWordWrap(True)
        lay.addWidget(info)
        tbl = table(["Self-test", "Result"])
        for name, supported, complete in r["monitors"]:
            if not supported:
                continue
            i = tbl.rowCount()
            tbl.insertRow(i)
            tbl.setItem(i, 0, item(name))
            tbl.setItem(i, 1, item("Complete" if complete else "Not finished yet", OK if complete else WARN))
        lay.addWidget(tbl, 1)
        close = button("Close", "primary")
        close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignRight)


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
        self.replay_btn = button("Open a recording")
        self.replay_btn.setToolTip("Play back a recording as a graph - handy for faults that come and go")
        self.replay_btn.clicked.connect(self.open_recording)
        self.bus_box = QComboBox()
        self.bus_box.currentIndexChanged.connect(self.change_bus)
        s.connected_changed.connect(lambda _: self.fill_buses())
        reload_btn = button("Reload list")
        reload_btn.clicked.connect(self.build)
        row.addWidget(self.btn)
        row.addWidget(self.rec_btn)
        row.addWidget(self.replay_btn)
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

    def open_recording(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open a recording", livedata.LOG_DIR, "Recordings (*.csv)")
        if not path:
            return
        try:
            times, cols = livedata.load_recording(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Hatchling", f"Couldn't open that recording: {e}")
            return
        if len(times) < 2:
            QMessageBox.information(self, "Hatchling", "That recording is empty.")
            return
        ReplayDialog(self, os.path.basename(path), times, cols).exec()


SERIES_COLORS = ["#E07A1F", "#2F7DD1", "#2FA36B", "#C2417A", "#7A5CC7", "#B59A1E", "#1FA2A8", "#8A8F98"]


class Chart(QWidget):
    """A recording as lines over time. Each value has its own scale (shown in the legend); move the mouse
    over the graph to read every value at that moment."""

    def __init__(self, times: List[float], cols: Dict[str, List[Optional[float]]]):
        super().__init__()
        self.times, self.cols = times, cols
        self.shown = list(cols)[:4]
        self.cursor: Optional[int] = None
        self.on_cursor: Callable[[Optional[int]], None] = lambda i: None
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self.ranges = {}
        for n, vals in cols.items():
            v = [x for x in vals if x is not None]
            lo, hi = (min(v), max(v)) if v else (0.0, 1.0)
            self.ranges[n] = (lo, hi if hi > lo else lo + 1)

    def color(self, name: str) -> str:
        return SERIES_COLORS[list(self.cols).index(name) % len(SERIES_COLORS)]

    def _area(self) -> QRectF:
        return QRectF(48, 12, max(10, self.width() - 60), max(10, self.height() - 40))

    def mouseMoveEvent(self, e):
        a = self._area()
        x = e.position().x()
        if a.left() <= x <= a.right():
            t = self.times[0] + (x - a.left()) / a.width() * (self.times[-1] - self.times[0])
            self.cursor = min(range(len(self.times)), key=lambda i: abs(self.times[i] - t))
        else:
            self.cursor = None
        self.update()
        self.on_cursor(self.cursor)

    def leaveEvent(self, e):
        self.cursor = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        a = self._area()
        t0, t1 = self.times[0], self.times[-1]
        span = (t1 - t0) or 1
        p.setPen(QPen(QColor(LINE), 1))
        for i in range(5):
            y = a.top() + i * a.height() / 4
            p.drawLine(QPointF(a.left(), y), QPointF(a.right(), y))
        p.setPen(QColor(MUTED))
        for i in range(5):
            t = t0 + i * span / 4
            x = a.left() + i * a.width() / 4
            p.drawText(QRectF(x - 40, a.bottom() + 4, 80, 18), Qt.AlignHCenter, f"{t:.0f} s")
        for n in self.shown:
            lo, hi = self.ranges[n]
            p.setPen(QPen(QColor(self.color(n)), 1.8))
            prev = None
            for t, v in zip(self.times, self.cols[n]):
                if v is None:
                    prev = None
                    continue
                pt = QPointF(a.left() + (t - t0) / span * a.width(), a.bottom() - (v - lo) / (hi - lo) * a.height())
                if prev is not None:
                    p.drawLine(prev, pt)
                prev = pt
        if self.cursor is not None:
            x = a.left() + (self.times[self.cursor] - t0) / span * a.width()
            p.setPen(QPen(QColor(INK), 1, Qt.DashLine))
            p.drawLine(QPointF(x, a.top()), QPointF(x, a.bottom()))


class ReplayDialog(QDialog):
    def __init__(self, parent, title: str, times, cols):
        super().__init__(parent)
        self.setWindowTitle(f"Recording - {title}")
        self.resize(980, 600)
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        hint = QLabel("Tick up to 8 values. Each line has its own scale.")
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        left.addWidget(hint)
        self.chart = Chart(times, cols)
        self.boxes = {}
        for n in cols:
            lo, hi = self.chart.ranges[n]
            cb = QCheckBox(f"{n}  ({lo:g} – {hi:g})")
            cb.setStyleSheet(f"color: {self.chart.color(n)}; font-weight: 600;")
            cb.setChecked(n in self.chart.shown)
            cb.toggled.connect(self.pick)
            self.boxes[n] = cb
            left.addWidget(cb)
        left.addStretch()
        self.readout = QLabel("Move the mouse over the graph to read the values.")
        self.readout.setWordWrap(True)
        self.readout.setMinimumWidth(240)
        left.addWidget(self.readout)
        close = button("Close", "primary")
        close.clicked.connect(self.accept)
        left.addWidget(close)
        lay.addLayout(left)
        self.chart.on_cursor = self.on_cursor
        lay.addWidget(self.chart, 1)
        self.times, self.cols = times, cols

    def pick(self, _):
        chosen = [n for n, cb in self.boxes.items() if cb.isChecked()][:8]
        self.chart.shown = chosen
        self.chart.update()

    def on_cursor(self, i):
        if i is None:
            return
        lines = [f"<b>{self.times[i]:.1f} s</b>"]
        for n in self.chart.shown:
            v = self.cols[n][i]
            lines.append(f"<span style='color:{self.chart.color(n)}'>{html.escape(n)}: "
                         f"<b>{'-' if v is None else f'{v:g}'}</b></span>")
        self.readout.setText("<br>".join(lines))


# ---------------------------------------------------------------- Feature finder
class FeatureFinderDialog(QDialog):
    """Shows bit-level differences (one or several modules) and saves them as a feature.

    groups: [(module, [ByteChange])] where 'before' is treated as off and 'after' as on,
    unless the user ticks that their car already has the feature (then it's flipped)."""

    def __init__(self, parent, s: Session, groups, title: str = "", before_label: str = "Before",
                 after_label: str = "After", offer_flip: bool = True, hint: str = "", name: str = ""):
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
        self.name = QLineEdit(name)
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
    return import_factory_path(parent, s, path)


def import_factory_path(parent, s: "Session", path: str) -> bool:
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


# ---------------------------------------------------------------- Share a factory file
class ShareFactoryDialog(QDialog):
    """Sends the car's factory file (VIN removed) plus the options it has, so cars with and without
    each option can be compared to find its bits."""

    def __init__(self, parent, s: Session):
        super().__init__(parent)
        self.s = s
        self.setWindowTitle("Share my factory file")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(8)
        t = QLabel("Help work out where features live")
        t.setObjectName("title")
        lay.addWidget(t)
        intro = QLabel("Your factory file shows how your car was built. Compared with other owners' files - cars "
                       "with and without each option - it shows exactly which settings each option uses, without "
                       "anyone changing anything on their car.<br><br><b>Your VIN is removed</b> before anything is "
                       "shared. Only the details below and the module settings are sent.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)
        grid = QGridLayout()
        info = s.vin_info
        self.f = {}
        for r, (key, label, val, ph) in enumerate((
                ("make", "Make", info.make if info and info.make in ("Ford", "Lincoln", "Mercury", "Mazda") else "Ford", ""),
                ("model", "Model", "", "Fiesta Mk7"),
                ("year", "Year", str(info.year) if info and info.year else "", "2015"),
                ("engine", "Engine", "", "1.0 EcoBoost 100"),
                ("trim", "Trim", "", "Zetec"),
                ("market", "Country sold in", "UK", "UK"))):
            w = QLineEdit(val)
            w.setPlaceholderText(ph)
            grid.addWidget(QLabel(label), r, 0)
            grid.addWidget(w, r, 1)
            self.f[key] = w
        lay.addLayout(grid)
        lab = QLabel("Which of these did your car come with from the factory?")
        lab.setObjectName("subtle")
        lay.addWidget(lab)
        og = QGridLayout()
        self.opts = []
        for i, o in enumerate(factory.OPTIONS):
            cb = QCheckBox(o)
            og.addWidget(cb, i // 3, i % 3)
            self.opts.append(cb)
        lay.addLayout(og)
        note = QLabel("Only tick what was fitted at the factory - not parts added later.")
        note.setObjectName("subtle")
        lay.addWidget(note)
        row = QHBoxLayout()
        row.addStretch()
        cancel, ok = button("Cancel"), button("Share on GitHub", "primary")
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.share)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def share(self):
        if not self.s.factory:
            QMessageBox.information(self, "Hatchling", "Import your factory file first.")
            return
        if not self.f["model"].text().strip():
            QMessageBox.information(self, "Hatchling", "Enter the model, e.g. Fiesta Mk7.")
            return
        if not library.repo():
            QMessageBox.information(self, "Hatchling", "Set the library's GitHub repository first (Feature library > "
                                                       "Library settings).")
            return
        anon = factory.anonymise(self.s.factory, {k: w.text() for k, w in self.f.items()},
                                 [cb.text() for cb in self.opts if cb.isChecked()])
        url, text, prefilled = library.factory_share_link(anon)
        QApplication.clipboard().setText(text)
        extra = (f"<br>{anon['dropped_lines']} line(s) that looked like they held a VIN were left out."
                 if anon["dropped_lines"] else "")
        QMessageBox.information(
            self, "Share my factory file",
            f"Ready: {len(anon['lines'])} lines, {len(anon['options'])} option(s) ticked, VIN removed.{extra}<br><br>"
            "GitHub opens in your browser. " + ("Everything is filled in" if prefilled else
                                                 "Click in <b>Factory file</b> and paste (Ctrl+V) - it's on your clipboard")
            + ", tick the box and press <b>Submit new issue</b>.")
        open_url(url)
        self.s.log("Opened GitHub to share the factory file")
        self.accept()


# ---------------------------------------------------------------- Learn a feature
class LearnDialog(QDialog):
    """Before snapshot -> the user changes one setting with another tool -> after snapshot -> feature.
    Works for anything you can change with another tool or at a dealer, on your own car."""

    def __init__(self, parent, s: Session, name: str = ""):
        super().__init__(parent)
        self.s = s
        self.setWindowTitle("Learn a feature from my car")
        self.setMinimumSize(560, 520)
        self.before: Dict[features.ModKey, asbuilt.Blocks] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        self.step = QLabel("")
        self.step.setObjectName("subtle")
        lay.addWidget(self.step)
        self.head = QLabel("")
        self.head.setObjectName("title")
        self.head.setWordWrap(True)
        lay.addWidget(self.head)
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        lay.addWidget(self.text)

        # step 1 widgets
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("What are you changing? e.g. Cruise control")
        lay.addWidget(self.name)
        self.mods = QListWidget()
        seen = set()
        for m in sorted(s.modules.values(), key=lambda m: (m.bus, m.addr)):
            if m.addr in seen or m.bus not in BUSES or BUSES[m.bus].protocol != "can":
                continue
            seen.add(m.addr)
            it = QListWidgetItem(f"{m.name}  ({m.bus}-CAN)")
            it.setData(Qt.UserRole, m.key)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.mods.addItem(it)
        lay.addWidget(self.mods, 1)

        # step 2 widgets
        self.dir_on = QCheckBox("I'm turning the feature ON (untick if you're turning it off)")
        self.dir_on.setChecked(True)
        lay.addWidget(self.dir_on)
        self.free_btn = button("Free the adapter for my other tool")
        self.free_btn.clicked.connect(self.free)
        lay.addWidget(self.free_btn, 0, Qt.AlignLeft)

        row = QHBoxLayout()
        row.addStretch()
        self.cancel = button("Cancel")
        self.cancel.clicked.connect(self.close_dialog)
        self.go_btn = button("", "primary")
        self.go_btn.clicked.connect(self.next)
        row.addWidget(self.cancel)
        row.addWidget(self.go_btn)
        lay.addLayout(row)
        self.stage = 1
        self.render()

    def render(self):
        s1 = self.stage == 1
        self.name.setVisible(s1)
        self.mods.setVisible(s1)
        self.dir_on.setVisible(not s1)
        self.free_btn.setVisible(not s1 and not self.s.simulated)
        if s1:
            self.step.setText("Step 1 of 3")
            self.head.setText("Take a 'before' snapshot")
            self.text.setText("Hatchling reads the settings of every ticked module and keeps a backup. Leave them all "
                              "ticked if you're not sure where the feature lives - it only takes a little longer.")
            self.go_btn.setText("Take the before snapshot")
        else:
            self.step.setText("Step 2 of 3")
            self.head.setText(f"Now change {html.escape(self.name.text() or 'the setting')}")
            self.text.setText(
                "Change just this one setting using the tool you normally use, or have it done at a garage. "
                "Changing more than one thing at once makes it hard to tell which bits belong to which.<br><br>"
                "<b>Using the same adapter?</b> Press <i>Free the adapter</i> first so the other program can use it. "
                "Close that program when you're done.<br><br>"
                "When the change is made, switch the ignition off for 30 seconds and back on, then press "
                "<b>Take the after snapshot</b>.")
            self.go_btn.setText("Take the after snapshot")

    def free(self):
        self.s.pause_adapter()
        self.free_btn.setText("Adapter freed - close your other program before continuing")
        self.free_btn.setEnabled(False)

    def selected(self) -> List[Module]:
        out = []
        for i in range(self.mods.count()):
            it = self.mods.item(i)
            if it.checkState() == Qt.Checked:
                m = self.s.modules.get(it.data(Qt.UserRole))
                if m:
                    out.append(m)
        return out

    def snapshot(self, then: Callable[[Dict[features.ModKey, asbuilt.Blocks]], None]):
        mods = self.selected()
        result: Dict[features.ModKey, asbuilt.Blocks] = {}
        jobs = {}
        for bus in {m.bus for m in mods}:
            ms = [m for m in mods if m.bus == bus]

            def job(ms=ms):
                out = {}
                for m in ms:
                    try:
                        out[(m.bus, m.addr)] = asbuilt.read_blocks(self.s.uds, m)
                    except (UdsError, ElmError) as e:
                        self.s.log(f"{m.name}: couldn't read - {e}")
                return out
            jobs[bus] = job
        self.go_btn.setEnabled(False)
        self.go_btn.setText("Reading the car...")

        def done():
            self.go_btn.setEnabled(True)
            then(result)

        def start(_=None):
            self.s.per_bus(jobs, lambda b, r: result.update(r), done)

        def failed(e):
            self.go_btn.setEnabled(True)
            QMessageBox.warning(self, "Hatchling", f"Couldn't reconnect to the adapter: {e}<br><br>Make sure the other "
                                                   "program is closed, then try again.")
            self.render()
        if self.s.paused:
            self.s.runner.run(self.s.resume_adapter, start, failed)
        else:
            start()

    def next(self):
        if self.stage == 1:
            if not self.name.text().strip():
                QMessageBox.information(self, "Hatchling", "Give the feature a name first.")
                return
            if not self.selected():
                QMessageBox.information(self, "Hatchling", "Tick at least one module.")
                return

            def got(blocks):
                if not blocks:
                    QMessageBox.warning(self, "Hatchling", "Couldn't read any modules. Check the adapter and try again.")
                    self.render()
                    return
                self.before = blocks
                for key, b in blocks.items():
                    asbuilt.save_backup(self.s.module_for(*key), b, self.s.vin, f"before learning {self.name.text()}")
                self.s.log(f"Learn: before snapshot of {len(blocks)} module(s)")
                self.stage = 2
                self.render()
            self.snapshot(got)
            return

        def got_after(after):
            groups = []
            for key, b in self.before.items():
                if key in after:
                    ch = finder.compare(b, after[key])
                    if ch:
                        groups.append((self.s.module_for(*key), ch))
            if not groups:
                QMessageBox.information(
                    self, "No changes found", "Nothing changed between the two snapshots. Check the change was saved "
                    "(and the ignition was cycled), then press Take the after snapshot again.")
                self.render()
                return
            if not self.dir_on.isChecked():   # turned OFF: before = on, after = off
                groups = [(m, [finder.ByteChange(c.did, c.byte, c.after, c.before) for c in ch]) for m, ch in groups]
            self.s.log(f"Learn: {sum(len(c) for _, c in groups)} byte(s) changed in {len(groups)} module(s)")
            self.step.setText("Step 3 of 3")
            self.hide()
            FeatureFinderDialog(
                self.parent(), self.s, groups, name=self.name.text().strip(),
                title=f"What changed when you {'turned on' if self.dir_on.isChecked() else 'turned off'} "
                      f"{self.name.text().strip()}",
                before_label="Off", after_label="On", offer_flip=False,
                hint="These are the bits that changed. If you only changed this one setting, keep them all. Save it "
                     "and Hatchling can switch it on and off from now on.").exec()
            self.close_dialog()
        self.snapshot(got_after)

    def close_dialog(self):
        if self.s.paused:
            self.s.runner.run(self.s.resume_adapter, lambda _: None,
                              lambda e: self.s.log(f"Couldn't reconnect the adapter: {e}"))
        self.close()


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
        frow.addSpacing(24)
        wlabel = QLabel("Whole car")
        wlabel.setObjectName("subtle")
        frow.addWidget(wlabel)
        self.fullbk_btn = button("Back up every module")
        self.fullbk_btn.clicked.connect(self.backup_everything)
        self.restore_btn = button("Restore a full backup")
        self.restore_btn.clicked.connect(self.restore_backup)
        self.tofactory_btn = button("Put everything back to factory")
        self.tofactory_btn.clicked.connect(self.restore_factory)
        for b in (self.fullbk_btn, self.restore_btn, self.tofactory_btn):
            frow.addWidget(b)
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

    # ---- whole car ---------------------------------------------------------
    def _scanned(self) -> Optional[List[Module]]:
        if not self.need_connection():
            return None
        mods = [m for m in self.s.modules.values() if m.bus in BUSES and BUSES[m.bus].protocol == "can"]
        if not mods:
            QMessageBox.information(self, "Hatchling", "Scan the car first (Modules > Scan the car).")
            return None
        return mods

    def backup_everything(self):
        if self._scanned() is None:
            return
        self.s.full_backup(lambda path: QMessageBox.information(self, "Full backup saved", path) if path else None,
                           note="manual")

    def restore_backup(self):
        mods = self._scanned()
        if mods is None:
            return
        start = safety.full_backups(self.s.vin)
        path, _ = QFileDialog.getOpenFileName(self, "Full backup to restore",
                                              start[0] if start else asbuilt.BACKUP_DIR, "Full backup (full-backup-*.json)")
        if not path:
            return
        try:
            info, saved = safety.load_full(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "Hatchling", f"Couldn't read that backup: {e}")
            return
        if info.get("vin") and self.s.vin and info["vin"] != self.s.vin:
            QMessageBox.warning(self, "Hatchling", f"That backup is from another car ({info['vin']}). Hatchling "
                                                 "only restores a car's own backups.")
            return
        by_addr = {a: blocks for _, a, _, blocks in saved}
        pairs = [(m, (lambda cur, a=m.addr: by_addr[a])) for m in self._one_per_addr(mods) if m.addr in by_addr]
        restore_all(self, self.s, f"backup from {info.get('taken', '?')}", pairs)

    def restore_factory(self):
        mods = self._scanned()
        if mods is None:
            return
        fac = self.s.factory
        if not fac:
            QMessageBox.information(self, "Hatchling", "Import your car's factory file first (Modules > Get my "
                                                       "factory file).")
            return
        pairs = [(m, (lambda cur, a=m.addr: factory.align(fac.blocks(a), cur)))
                 for m in self._one_per_addr(mods) if m.addr in fac.modules]
        restore_all(self, self.s, "factory settings", pairs)

    def _one_per_addr(self, mods: List[Module]) -> List[Module]:
        cur = self.s.elm.bus if self.s.elm else ""
        out: Dict[int, Module] = {}
        for m in sorted(mods, key=lambda m: m.bus != cur):
            out.setdefault(m.addr, m)
        return list(out.values())

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
        self.s.before_write(lambda: self.s.ensure_bus(m.bus, lambda: self.s.runner.run(
            lambda: asbuilt.safe_write(self.s.uds, m, orig, target, self.s.vin, self.s.log), ok)))


def restore_all(parent, s: "Session", what: str, pairs: List[Tuple[Module, Callable[[asbuilt.Blocks], asbuilt.Blocks]]]):
    """Reads each module, works out what differs from `what`, asks once, then writes and checks every module."""
    if not pairs:
        QMessageBox.information(parent, "Hatchling", f"None of the scanned modules are in the {what}.")
        return
    plan: List[Tuple[Module, asbuilt.Blocks, asbuilt.Blocks]] = []

    def read_bus(mods):
        out = []
        for m, wanted in mods:
            try:
                cur = asbuilt.read_blocks(s.uds, m)
            except (UdsError, ElmError) as e:
                s.log(f"{m.name}: couldn't read - {e}")
                continue
            targets = safety.restore_targets(cur, wanted(cur))
            if targets:
                out.append((m, cur, {**cur, **targets}))
        return out
    jobs = {b: (lambda b=b: read_bus([p for p in pairs if p[0].bus == b])) for b in {m.bus for m, _ in pairs}}

    def planned():
        if not plan:
            QMessageBox.information(parent, "Hatchling", f"Every module already matches the {what}. Nothing to do.")
            return
        lines = "".join(f"<tr><td>{html.escape(m.name)}</td><td>{len(asbuilt.diff(cur, tgt))} byte(s)</td></tr>"
                        for m, cur, tgt in plan)
        if QMessageBox.question(parent, "Restore", f"<b>Put {len(plan)} module(s) back to the {what}?</b><br><br>"
                                f"<table cellspacing=6>{lines}</table><br>Each module is backed up before it's "
                                "written and checked afterwards. Keep the ignition on and a charger connected.") \
                != QMessageBox.Yes:
            return
        results = []
        wjobs = {b: (lambda b=b: [(m, asbuilt.safe_write(s.uds, m, cur, tgt, s.vin, s.log))
                                  for m, cur, tgt in plan if m.bus == b]) for b in {m.bus for m, _, _ in plan}}

        def written():
            bad = [f"{m.name}: {r.message}" for m, r in results if not r.verified]
            s.record(history.WRITE, f"Restored {len(results)} module(s) to the {what}"
                     + (f" - {len(bad)} failed" if bad else ""), {"modules": [f"{m.addr:03X}" for m, _ in results]})
            if bad:
                QMessageBox.critical(parent, "Restore", "Some modules didn't verify:<br>" + "<br>".join(bad))
            else:
                SuccessDialog(parent, "Restored", [f"{len(results)} module(s) put back to the {what}.",
                                                   "Turn the ignition off for 30 seconds, then back on."]).exec()
        s.before_write(lambda: s.per_bus(wjobs, lambda b, r: results.extend(r), written))
    s.per_bus(jobs, lambda b, r: plan.extend(r), planned)


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


class WishListDialog(QDialog):
    """Features owners want that nobody has found the settings for yet. Pick one to look for on your car."""
    STATUS = {"wanted": "Wanted", "being found": "Someone's looking", "in library": "In the library"}

    def __init__(self, parent, s: "Session"):
        super().__init__(parent)
        self.s = s
        self.setWindowTitle("Wish list")
        self.setMinimumSize(760, 480)
        lay = QVBoxLayout(self)
        intro = QLabel("Features people want switched on that aren't in the library yet. If your car has one (or you "
                       "can switch it on another way, like the car's own menu), you can find its settings with "
                       "<b>Learn a feature</b> - nothing is written to your car while you do. Then share it and "
                       "everyone with the same car can turn it on.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.items = library.wishlist()
        self.tbl = table(["Feature", "Car", "Where it's likely set", "Status"])
        for w in self.items:
            r = self.tbl.rowCount()
            self.tbl.insertRow(r)
            st = w.get("status", "wanted")
            for c, v in enumerate([w["name"], w.get("models", ""), w.get("modules_hint", ""),
                                   self.STATUS.get(st, st)]):
                self.tbl.setItem(r, c, item(v, OK if c == 3 and st == "in library" else
                                            WARN if c == 3 and st == "being found" else None))
        self.tbl.currentCellChanged.connect(lambda r, *_: self.show_item(r))
        lay.addWidget(self.tbl, 1)
        self.about = QLabel("Pick a feature to see what it does.")
        self.about.setWordWrap(True)
        self.about.setObjectName("subtle")
        lay.addWidget(self.about)
        row = QHBoxLayout()
        claim = button("I'll look for this")
        claim.setToolTip("Opens a post on GitHub so others know you're on it and can help")
        claim.clicked.connect(self.claim)
        learn = button("Learn it on my car", "primary")
        learn.clicked.connect(self.learn)
        req = button("Request something else")
        req.clicked.connect(lambda: self._open(lambda: library.request_feature_link(self.car())))
        close = button("Close")
        close.clicked.connect(self.accept)
        for b in (claim, learn, req):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(close)
        lay.addLayout(row)
        if not self.items:
            self.about.setText("The wish list hasn't downloaded yet - press Check for new features first.")

    def car(self) -> str:
        return self.s.vin_info.summary if self.s.vin_info else ""

    def current(self) -> Optional[dict]:
        r = self.tbl.currentRow()
        return self.items[r] if 0 <= r < len(self.items) else None

    def show_item(self, r):
        w = self.items[r] if 0 <= r < len(self.items) else None
        if w:
            self.about.setText(f"<b>{html.escape(w['name'])}</b> - {html.escape(w.get('description', ''))}")

    def _open(self, url_fn):
        try:
            open_url(url_fn())
        except library.LibraryError as e:
            QMessageBox.information(self, "Hatchling", str(e))

    def claim(self):
        w = self.current()
        if not w:
            QMessageBox.information(self, "Hatchling", "Pick a feature first.")
            return
        self._open(lambda: library.find_feature_link(w, self.car()))

    def learn(self):
        w = self.current()
        if not w:
            QMessageBox.information(self, "Hatchling", "Pick a feature first.")
            return
        self.accept()
        self.s.open_learn(w["name"])


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
        wish_btn = button("Wish list - help find features")
        wish_btn.clicked.connect(lambda: WishListDialog(self, self.s).exec())
        self.refresh_btn.clicked.connect(self.refresh)
        self.install_btn.clicked.connect(self.install)
        self.only_mine.toggled.connect(self.render)
        settings_btn.clicked.connect(self.edit_settings)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.install_btn)
        row.addSpacing(12)
        row.addWidget(self.only_mine)
        row.addStretch()
        row.addWidget(wish_btn)
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
            self.s.library_changed.emit()
            self.s.log(f"Library updated: {len(idx['features'])} feature(s)")
            if not quiet:
                self.show_status("up to date" + (f", {new} new" if new > 0 else ""))

        def err(e):
            self.show_status(f"<span style='color:{WARN}'>offline - {e}</span>" if not quiet else "")
        self.s.net.run(lambda: (library.update(), library.update_extras())[0], ok, err)

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
            self.s.library_changed.emit()
            self.render()
            QMessageBox.information(
                self, "Installed", f"<b>{e['name']}</b> is in your Features list.<br><br>Run a <b>module scan</b> to "
                "see whether it's on, then switch it on from <b>Features</b>.<br><br>Once it works on your car, "
                "press <b>Mark as working on my car</b>.")
        self.s.runner.run(lambda: library.get(e), ok)


# ---------------------------------------------------------------- Community
class CommunityPage(Page):
    def __init__(self, s):
        super().__init__(s, "Community", "What other owners are asking, sharing and reporting. Reading needs no "
                                         "account - posting opens GitHub, where a free account lets you join in.")
        row = QHBoxLayout()
        ask = button("Ask a question", "primary")
        ask.clicked.connect(lambda: self.open_path("discussions/new?category=q-a"))
        browse = button("Browse discussions")
        browse.clicked.connect(lambda: self.open_path("discussions"))
        fb = button("Give feedback or report a problem")
        fb.clicked.connect(lambda: self.open_path("issues/new?template=feedback.yml"))
        req = button("Request a feature")
        req.clicked.connect(lambda: open_url(library.request_feature_link(self.car())) if library.repo() else
                            self.open_path(""))
        self.show_what = QComboBox()
        self.show_what.addItem("Everything", "")
        self.show_what.addItem("Most wanted requests", "feature-request")
        self.show_what.addItem("Fault code fixes", "code-fix")
        self.show_what.addItem("Features being looked for", "finding-feature")
        self.show_what.currentIndexChanged.connect(lambda _: self.refresh())
        self.refresh_btn = button("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        for b in (ask, browse, fb, req):
            row.addWidget(b)
        row.addStretch()
        row.addWidget(self.show_what)
        row.addWidget(self.refresh_btn)
        self.root.addLayout(row)
        self.status = QLabel("")
        self.status.setObjectName("subtle")
        self.root.addWidget(self.status)
        self.tbl = table(["What", "Title", "By", "Votes", "Replies", "Updated"])
        self.tbl.itemDoubleClicked.connect(lambda _: self.open_current())
        self.root.addWidget(self.tbl, 1)
        tip = QLabel("Double-click a post to open it. To vote for a request, open it and press 👍 on GitHub - "
                     "the most wanted are worked on first.")
        tip.setObjectName("subtle")
        self.root.addWidget(tip)
        self.posts: List[dict] = []
        self.loaded = False

    def showEvent(self, e):
        if not self.loaded:
            self.loaded = True
            self.refresh()
        super().showEvent(e)

    def open_path(self, path: str):
        r = library.repo()
        if not r:
            QMessageBox.information(self, "Hatchling", "Set the GitHub repository first (Feature library > "
                                                       "Library settings).")
            return
        open_url(f"https://github.com/{r}/{path}")

    def car(self) -> str:
        return self.s.vin_info.summary if self.s.vin_info else ""

    def open_current(self):
        r = self.tbl.currentRow()
        if 0 <= r < len(self.posts) and self.posts[r]["url"].startswith("https://github.com/"):
            open_url(self.posts[r]["url"])

    def refresh(self):
        r = library.repo()
        if not r:
            self.status.setText("Set the GitHub repository in Feature library > Library settings.")
            return
        self.status.setText("Loading...")
        label = self.show_what.currentData() or ""

        def ok(posts):
            self.posts = posts
            self.tbl.setRowCount(0)
            for p in posts:
                i = self.tbl.rowCount()
                self.tbl.insertRow(i)
                kind = online.post_kind(p["labels"])
                vals = [kind, p["title"] + ("" if p["state"] == "open" else "  (closed)"), p["by"],
                        str(p.get("votes", 0) or ""), str(p["comments"]), p["updated"]]
                for c, v in enumerate(vals):
                    self.tbl.setItem(i, c, item(v, ACCENT if c == 0 else MUTED if p["state"] != "open" and c == 1 else None))
            self.status.setText(f"{len(posts)} recent post(s)" if posts else
                                "Nothing yet - be the first to ask a question or give feedback.")

        def err(e):
            self.status.setText(f"Couldn't load posts - {e}. You can still use the buttons above.")
        self.s.net.run(lambda: online.community_posts(r, label=label), ok, err)


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
            it = QListWidgetItem(f"{p.name}\n     {p.category}" + ("  ·  waiting for values" if p.needs_values else ""),
                                 self.list)
            if p.needs_values:
                it.setForeground(QColor(MUTED))
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
        self.run_btn.setText("Run procedure")
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
        if p.needs_values:
            pre = "".join(f"<br>• {c}" for c in p.preconditions)
            self.meta.setText(f"<span style='color:{WARN}'>Waiting for values</span> · {p.module_label} · "
                              f"{p.vehicle}<br><br>Hatchling knows this job but not yet the commands the car "
                              "expects for it, so it can't run it. If you have a tool that does this job, share it "
                              "in the Community - or request it so others can vote."
                              f"{'<br><br><b>When it runs it will need:</b>' + pre if pre else ''}")
            self.run_btn.setText("Request this job")
            self.confirm.hide()
            return
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
        if not p:
            return
        if p != self.RESTART and p.needs_values:
            try:
                open_url(library.request_feature_link(self.s.vin_info.summary if self.s.vin_info else "", p.name))
            except library.LibraryError as e:
                QMessageBox.information(self, "Hatchling", str(e))
            return
        if not self.need_connection():
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
        start = lambda: self.s.ensure_bus(p.bus, lambda: self._step(p, 0))
        if any(st.type in ("write", "routine", "reset") for st in p.steps):
            self.s.before_write(start)
        else:
            start()

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
        self.touched = False
        self.toggle.toggled.connect(lambda _: (setattr(self, "touched", True), changed()))
        lay.addWidget(self.toggle)
        self.actual: Optional[str] = None

    def set_actual(self, state: str):
        self.actual = state
        self.toggle.blockSignals(True)
        self.toggle.setChecked(state == features.ENABLED)
        self.toggle.blockSignals(False)
        self.toggle.setEnabled(state in (features.ENABLED, features.DISABLED, features.MIXED))
        self.touched = False
        self.refresh()

    @property
    def pending(self) -> bool:
        if not self.toggle.isEnabled() or self.actual is None:
            return False
        if self.actual == features.MIXED:
            return self.touched          # partly set: any choice the user makes is a change (fully on or off)
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
        self.list.setMinimumWidth(260)
        self.list.currentRowChanged.connect(self.show_feature)
        split.addWidget(self.list)
        self.entries: List[Tuple[str, object]] = []   # per list row: ("head", text) / ("feat", Feature) / ("lib", entry)

        self.right_stack = QStackedWidget()
        right = QWidget()
        self.right_stack.addWidget(right)
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
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("subtle")
        rl.addWidget(self.hint)
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
        lrow = QHBoxLayout()
        learn_btn = button("Learn a feature from my car")
        learn_btn.setObjectName("link")
        learn_btn.clicked.connect(lambda: self.s.open_learn())
        fac_btn = button("Find what's changed since the factory")
        fac_btn.setObjectName("link")
        fac_btn.clicked.connect(lambda: self.s.open_factory_diff())
        lrow.addWidget(learn_btn)
        lrow.addSpacing(16)
        lrow.addWidget(fac_btn)
        lrow.addStretch()
        rl.addLayout(lrow)

        # library entry view (feature available for this car but not installed)
        libw = QWidget()
        ll = QVBoxLayout(libw)
        ll.setContentsMargins(14, 0, 0, 0)
        lc, lcl = card()
        self.lib_title = QLabel("")
        self.lib_title.setObjectName("title")
        self.lib_text = QLabel("")
        self.lib_text.setWordWrap(True)
        self.lib_text.setTextFormat(Qt.RichText)
        lcl.addWidget(self.lib_title)
        lcl.addWidget(self.lib_text)
        ll.addWidget(lc)
        lb = QHBoxLayout()
        self.lib_add = button("Add to my features", "primary")
        self.lib_add.clicked.connect(self.add_from_library)
        lb.addWidget(self.lib_add)
        lb.addStretch()
        ll.addLayout(lb)
        ll.addStretch()
        self.right_stack.addWidget(libw)

        split.addWidget(self.right_stack)
        split.setStretchFactor(1, 1)
        self.root.addWidget(split, 1)

        self.feats: List[features.Feature] = []
        self.rows: List[PartRow] = []
        self.states: Dict[str, Dict[features.ModKey, str]] = {}   # feature id -> part states
        self.hw: Dict[str, List[Optional[Tuple[bool, str]]]] = {}  # feature id -> result per check
        self.blocks: Dict[features.ModKey, asbuilt.Blocks] = {}
        s.connected_changed.connect(lambda _: (self.states.clear(), self.hw.clear(), self.reload()))
        s.features_changed.connect(self.reload)
        s.features_detected.connect(self.on_detected)
        s.modules_changed.connect(lambda: self.refresh_list())
        s.library_changed.connect(lambda: self.refresh_list())
        self.reload()

    # ---- list -------------------------------------------------------
    def reload(self):
        keep = self.current_key()
        self.feats = [f for f in features.load_all()
                      if f.category != "Simulator" or self.s.simulated or not self.s.connected]
        self.states = {k: v for k, v in self.states.items() if any(f.id == k for f in self.feats)}
        self.hw.clear()
        self.refresh_list(keep)

    def current_key(self) -> Optional[str]:
        r = self.list.currentRow()
        if 0 <= r < len(self.entries):
            kind, obj = self.entries[r]
            if kind == "feat":
                return "feat:" + obj.id
            if kind == "lib":
                return "lib:" + obj["id"]
        return None

    def library_entries(self) -> List[dict]:
        """Library features for this car that aren't installed yet (only once the car is known)."""
        idx, _ = library.best_index()
        if not idx or not self.s.connected:
            return []
        have = set(library.installed()) | {f.library_id for f in self.feats if f.library_id}
        make = self.s.vin_info.make if self.s.vin_info else None
        out = []
        for e in idx.get("features", []):
            if e["id"] in have or not library.entry_matches_make(e, make):
                continue
            if self.s.modules:
                lvl = self.lib_level(e)
                if lvl in (features.DIFFERENT, features.MISSING):
                    continue
            out.append(e)
        return out

    def lib_level(self, e: dict) -> str:
        f = features._parse({"name": e["name"], "modules": [dict(m, changes=[]) for m in e["modules"]]}, e["id"])
        return features.compatibility_level(features.compatibility(f, self.scanned_identities()))

    def refresh_list(self, keep: Optional[str] = None):
        keep = keep if keep is not None else self.current_key()
        groups = [("On", [features.ENABLED]), ("Partly on", [features.MIXED]), ("Off", [features.DISABLED]),
                  ("Not checked yet", ["not read"]), ("Not on this car", [features.NOT_FOUND, features.UNKNOWN]),
                  ("Waiting for values", [features.NEEDS_VALUES])]
        by: Dict[str, List[features.Feature]] = {}
        for f in self.feats:
            by.setdefault(self.feature_state(f), []).append(f)
        self.list.blockSignals(True)
        self.list.clear()
        self.entries = []

        def head(text):
            it = QListWidgetItem(text)
            it.setFlags(Qt.NoItemFlags)
            fnt = it.font()
            fnt.setBold(True)
            it.setFont(fnt)
            it.setForeground(QColor(MUTED))
            self.list.addItem(it)
            self.entries.append(("head", text))

        for title, states in groups:
            items = [f for st in states for f in by.get(st, [])]
            if not items:
                continue
            head(title)
            for f in sorted(items, key=lambda f: f.name):
                st = self.feature_state(f)
                it = QListWidgetItem(f"  {f.name}")
                it.setForeground(QColor(STATE_COLORS.get(st, INK)) if st in (features.ENABLED, features.MIXED)
                                 else QColor(INK))
                self.list.addItem(it)
                self.entries.append(("feat", f))
        libs = self.library_entries()
        if libs:
            head("In the library for your car")
            for e in libs:
                it = QListWidgetItem(f"  {e['name']}")
                it.setForeground(QColor(ACCENT))
                self.list.addItem(it)
                self.entries.append(("lib", e))
        self.list.blockSignals(False)
        target = 0
        for i, (kind, obj) in enumerate(self.entries):
            key = "feat:" + obj.id if kind == "feat" else "lib:" + obj["id"] if kind == "lib" else None
            if keep and key == keep:
                target = i
                break
        else:
            target = next((i for i, (k, _) in enumerate(self.entries) if k != "head"), -1)
        if target >= 0:
            self.list.setCurrentRow(target)
        self.show_feature()
        if not self.entries:
            self.title.setText("No features yet")
            self.desc.setText("Scan the car, then use Learn a feature from my car, or add one from the Feature "
                              "library.")

    def feature(self) -> Optional[features.Feature]:
        r = self.list.currentRow()
        if 0 <= r < len(self.entries) and self.entries[r][0] == "feat":
            return self.entries[r][1]
        return None

    def lib_entry(self) -> Optional[dict]:
        r = self.list.currentRow()
        if 0 <= r < len(self.entries) and self.entries[r][0] == "lib":
            return self.entries[r][1]
        return None

    def confidence(self, f: features.Feature) -> str:
        """How sure we are this feature is right for this car's modules (library features)."""
        if not f.library_id:
            return ""
        idx, _ = library.best_index()
        e = next((x for x in (idx or {}).get("features", []) if x["id"] == f.library_id), None)
        if not e:
            return ""
        return self.confidence_text(e)

    def confidence_text(self, e: dict) -> str:
        fam = e.get("confirmations_by_family") or {}
        mine = {features.part_family(m.identity.get("Part number", "")) for m in self.s.modules.values()
                if m.identity.get("Part number")}
        n_mine = sum(v for k, v in fam.items() if k in mine)
        total = e.get("confirmations", 0)
        if n_mine >= 3:
            return f"<span style='color:{OK}'>Confirmed working on {n_mine} cars with your module</span>"
        if n_mine:
            return f"<span style='color:{WARN}'>Early - confirmed on {n_mine} car(s) with your module, test carefully</span>"
        if total:
            return f"<span style='color:{WARN}'>Confirmed on {total} car(s), none with your exact module yet</span>"
        return f"<span style='color:{WARN}'>New - not confirmed by anyone yet, test carefully</span>"

    def show_library_entry(self, e: dict):
        self.right_stack.setCurrentIndex(1)
        self.lib_title.setText(e["name"])
        lvl = self.lib_level(e) if self.s.modules else ""
        match = {features.SAME: f"<span style='color:{OK}'>Your modules match the ones it was learned on</span>",
                 features.FAMILY: f"<span style='color:{OK}'>Same modules, different revision - very likely fine</span>",
                 features.UNKNOWN_PART: "Couldn't compare part numbers"}.get(lvl, "Scan the car to check it "
                                                                              "matches your modules")
        self.lib_text.setText(
            f"<p>{html.escape(e.get('description') or '')}</p>"
            f"<p>{match}<br>{self.confidence_text(e)}</p>"
            f"<p><b>Parts needed:</b> {html.escape(e.get('hardware_required') or 'none listed')}<br>"
            f"<b>Learned on:</b> {html.escape(e.get('learned_on') or '-')}</p>"
            "<p>Add it to your features, then Hatchling reads your car to show whether it's on.</p>")

    def add_from_library(self):
        e = self.lib_entry()
        if not e:
            return

        def ok(data):
            try:
                library.install(data)
            except (library.LibraryError, OSError, ValueError, KeyError) as ex:
                QMessageBox.warning(self, "Hatchling", f"Couldn't add it: {ex}")
                return
            self.s.log(f"Added from library: {e['name']}")
            self.reload()
            for i, (kind, obj) in enumerate(self.entries):
                if kind == "feat" and obj.library_id == e["id"]:
                    self.list.setCurrentRow(i)
                    break
            if self.s.connected:
                self.read_status()
        self.s.runner.run(lambda: library.get(e), ok)

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
        e = self.lib_entry()
        if e:
            self.show_library_entry(e)
            return
        self.right_stack.setCurrentIndex(0)
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
                          + (f"<br>{self.confidence(f)}" if self.confidence(f) else "")
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
        self.apply_btn.setEnabled(True)
        st = self.feature_state(f)
        self.hint.setText(self.next_step(f, st, n)[0])
        hint = {"not read": "Hatchling hasn't read the car yet",
                features.NOT_FOUND: "This car doesn't have a module this feature needs",
                features.NEEDS_VALUES: "Hatchling doesn't know where this is set on your car yet"}.get(st, "")
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
                r.touched = True
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
            self.s.before_write(lambda: self.s.per_bus(jobs, each, done))
        self._read_parts(parts, got_fresh)

    def next_step(self, f: features.Feature, st: str, pending: int) -> Tuple[str, Optional[str]]:
        """(plain explanation of what's needed next, action key or None if ready to apply)."""
        if not self.s.connected:
            return "Connect to the car first.", "connect"
        if st == features.NEEDS_VALUES:
            return ("Hatchling doesn't know where this feature is set on your car yet. Learn it from your car "
                    "(or add it from the library) and the switches will unlock."), "learn"
        if st in (features.NOT_FOUND, features.UNKNOWN):
            return "Your car doesn't have a module this feature needs, or it didn't answer.", "none"
        if st == "not read":
            return "Hatchling needs to read the car to see whether this is on.", "read"
        if not pending:
            now = "on" if st == features.ENABLED else "off" if st == features.DISABLED else "partly on"
            return (f"It's {now} right now. Flip the main switch (or a module's switch) to change it, then press "
                    "Apply changes."), "none"
        return f"Ready - press Apply changes to write {pending} module(s). A backup is made first.", None

    def apply_changes(self):
        f = self.feature()
        if not f:
            return
        rows_pending = sum(r.pending for r in self.rows)
        msg, action = self.next_step(f, self.feature_state(f), rows_pending)
        if action == "connect":
            if QMessageBox.question(self, "Hatchling", msg + " Go to Connection now?") == QMessageBox.Yes:
                self.s.go("Connect")
            return
        if action == "learn":
            if QMessageBox.question(self, "Hatchling", msg + "<br><br>Learn it from your car now?") == QMessageBox.Yes:
                self.s.open_learn(f.name)
            return
        if action == "read":
            if QMessageBox.question(self, "Hatchling", msg + " Read it now?") == QMessageBox.Yes:
                self.read_status()
            return
        if action == "none":
            QMessageBox.information(self, "Hatchling", msg)
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
    upd_progress = Signal(object, object)

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
            ("Setup", [("Connect", "Connection", ConnectPage), ("Community", "Community", CommunityPage)]),
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
        self.s.go = self.go
        self.s.open_learn = self.open_learn
        self.s.open_factory_diff = self.open_factory_diff
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

    def open_learn(self, name: str = ""):
        if not self.s.connected:
            QMessageBox.information(self, "Hatchling", "Connect to the car first, then scan it.")
            self.go("Connect")
            return
        if not self.s.modules:
            QMessageBox.information(self, "Hatchling", "Scan the car first so Hatchling knows its modules.")
            self.go("Modules")
            return
        LearnDialog(self, self.s, name).show()

    def open_factory_diff(self):
        self.go("Modules")
        page = self.pages["Modules"]
        if not self.s.factory:
            QMessageBox.information(self, "Hatchling", "Import your car's factory file first (Modules > Import "
                                                       "factory file), then scan the car.")
            return
        page.changes_since_factory()

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

    # ---- updates: found, downloaded and installed from inside the app ----------------
    def check_updates(self):
        self.update_path = ""
        self.update_btn: Optional[QPushButton] = None

        def ok(rel):
            if rel is None:
                updates.clean_old()
                return
            self.s.log(f"Update available: Hatchling {rel.version}")
            if rel.installer and rel.sha256 and sys.platform == "win32":
                self.download_update(rel)
            else:   # nothing we can install ourselves - fall back to the download page
                self.show_update_button(f"Update available: v{rel.version}",
                                        lambda: open_url(rel.page or rel.url), rel.notes)
        self.s.net.run(updates.check, ok, lambda e: self.s.log(f"Update check skipped: {e}"))

    def show_update_button(self, text: str, on_click, tip: str = ""):
        if self.update_btn is None:
            self.update_btn = button(text, "primary")
            self.statusBar().addPermanentWidget(self.update_btn)
        else:
            self.update_btn.setText(text)
            try:
                self.update_btn.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.update_btn.setToolTip(tip)
        self.update_btn.setEnabled(on_click is not None)
        if on_click:
            self.update_btn.clicked.connect(on_click)

    def download_update(self, rel):
        self.show_update_button(f"Downloading update v{rel.version}…", None)
        prog = lambda done, total: self.upd_progress.emit(done, total)
        self._upd_version = rel.version
        if not getattr(self, "_upd_hooked", False):
            self._upd_hooked = True
            self.upd_progress.connect(lambda d, t: self.update_btn and self.update_btn.setText(
                f"Downloading update v{self._upd_version}… {100 * d // t}%" if t else
                f"Downloading update v{self._upd_version}… {d // (1024 * 1024)} MB"))

        def ok(path):
            self.update_path = path
            self.s.log(f"Update v{rel.version} downloaded and checked: {path}")
            self.show_update_button(f"Restart to update to v{rel.version}", self.install_update_now,
                                    f"What's new:\n{rel.notes}\n\nIf you don't restart now, the update installs "
                                    "when you close Hatchling.")

        def err(e):
            self.s.log(f"Update download failed: {e}")
            self.show_update_button(f"Update v{rel.version}: try again", lambda: self.download_update(rel),
                                    f"The download didn't finish ({e}). Click to try again.")
        self.s.net.run(lambda: updates.download(rel, prog), ok, err)

    def install_update_now(self):
        if self.s.runner.busy:
            QMessageBox.information(self, "Hatchling", "Hatchling is still talking to the car. Try again when "
                                                       "it's finished.")
            return
        try:
            updates.install(self.update_path, relaunch=True)
        except updates.UpdateError as e:
            QMessageBox.warning(self, "Hatchling", f"The update couldn't start: {e}")
            return
        self.s.log("Installing the update - Hatchling will reopen when it's done")
        self.update_path = ""   # don't start it a second time on close
        self.close()

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
        if getattr(self, "update_path", "") and library.settings().get("install_updates_on_close", True):
            try:   # downloaded but not installed yet: install quietly now, without reopening
                updates.install(self.update_path, relaunch=False)
            except updates.UpdateError:
                pass
        self.s.runner.pool.shutdown(wait=False, cancel_futures=True)
        self.s.net.pool.shutdown(wait=False, cancel_futures=True)
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
