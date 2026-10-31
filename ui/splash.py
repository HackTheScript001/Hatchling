"""Animated startup splash: the hatchback draws itself in, wheels pop on, a diagnostic pulse
sweeps underneath, then the name and a loading bar."""
from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.logo_shape import BODY, PULSE, WHEEL_R, WHEELS, WINDOW_FRONT, WINDOW_REAR

from ui.theme import AMBER, HEAD_FONT, BODY_FONT, T

INK = QColor(T["INK"])
BG = QColor(T["PANEL"])
EDGE = QColor(T["LINE"])
MUTED = QColor(T["MUTED"])
TRACK = QColor(T["LINE"])
SIGNAL = QColor(AMBER)
DURATION_MS = 2400


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def phase(t: float, start: float, end: float) -> float:
    return ease((t - start) / (end - start))


def _partial(points, frac):
    """The first `frac` (0..1) of a polyline, measured by length."""
    segs = list(zip(points, points[1:]))
    lens = [math.dist(p, q) for p, q in segs]
    left = sum(lens) * max(0.0, min(1.0, frac))
    out = [points[0]]
    for (p, q), ln in zip(segs, lens):
        if left <= 0:
            break
        if left >= ln:
            out.append(q)
            left -= ln
        else:
            if ln:
                k = left / ln
                out.append((p[0] + (q[0] - p[0]) * k, p[1] + (q[1] - p[1]) * k))
            break
    return out


def draw_logo(p: QPainter, rect: QRectF, t: float = 1.0):
    """Draws the hatchback logo into rect. t in 0..1 animates it (1 = complete)."""
    s, ox, oy = rect.width(), rect.x(), rect.y()
    P = lambda pts: [QPointF(ox + x * s, oy + y * s) for x, y in pts]
    p.setRenderHint(QPainter.Antialiasing)

    # body outline traces itself from the rear bumper round to the nose
    body = BODY + [BODY[0]]
    b = phase(t, 0.0, 0.45)
    if b > 0:
        fill = phase(t, 0.35, 0.6)
        if fill > 0:
            c = QColor(INK)
            c.setAlphaF(0.22 * fill)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPolygon(P(WINDOW_REAR))
            p.drawPolygon(P(WINDOW_FRONT))
        pen = QPen(INK, s * 0.042, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(P(_partial(body, b)))

    # wheels pop on, rear then front
    r = WHEEL_R * s
    for i, (x, y) in enumerate(WHEELS):
        w = phase(t, 0.42 + i * 0.08, 0.62 + i * 0.08)
        if w <= 0:
            continue
        pop = w * (1 + 0.22 * math.sin(math.pi * w))
        c = QPointF(ox + x * s, oy + y * s)
        p.setPen(Qt.NoPen)
        p.setBrush(BG)
        p.drawEllipse(c, r * 1.22 * pop, r * 1.22 * pop)
        p.setPen(QPen(INK, s * 0.034))
        p.drawEllipse(c, r * pop, r * pop)
        p.setPen(Qt.NoPen)
        p.setBrush(INK)
        p.drawEllipse(c, r * 0.34 * pop, r * 0.34 * pop)

    # diagnostic pulse sweeps left to right, dot rides the tip
    k = phase(t, 0.6, 0.95)
    if k > 0:
        pts = _partial(PULSE, k)
        p.setPen(QPen(SIGNAL, s * 0.03, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(P(pts))


class Splash(QWidget):
    def __init__(self, version: str, on_done):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(520, 340)
        self.version = version
        self.on_done = on_done
        self.status = "Starting up"
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)

    def start(self):
        screen = self.screen().availableGeometry()
        self.move(screen.center() - self.rect().center())
        self.show()
        self.clock.start()
        self.timer.start()

    def set_status(self, text: str):
        self.status = text
        self.update()

    def tick(self):
        self.update()
        if self.clock.elapsed() >= DURATION_MS + 250:
            self.timer.stop()
            self.close()
            self.on_done()

    def paintEvent(self, _):
        t = min(1.0, self.clock.elapsed() / DURATION_MS) if self.clock.isValid() else 0
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(EDGE, 1))
        p.setBrush(BG)
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 8, 8)

        draw_logo(p, QRectF(self.width() / 2 - 80, 36, 160, 160), t)

        # name and tagline fade up
        f = phase(t, 0.62, 0.9)
        rise = (1 - f) * 12
        c = QColor(INK)
        c.setAlphaF(f)
        p.setPen(c)
        font = QFont(HEAD_FONT, 28)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.drawText(QRectF(0, 200 + rise, self.width(), 44), Qt.AlignHCenter, "Hatchling")
        c2 = QColor(MUTED)
        c2.setAlphaF(f)
        p.setPen(c2)
        p.setFont(QFont(BODY_FONT, 10))
        p.drawText(QRectF(0, 246 + rise, self.width(), 20), Qt.AlignHCenter, "Diagnostics for your Ford")

        # loading bar + status
        bar = QRectF(150, 290, 220, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(TRACK)
        p.drawRoundedRect(bar, 2, 2)
        p.setBrush(SIGNAL)
        p.drawRoundedRect(QRectF(bar.x(), bar.y(), bar.width() * ease(t), bar.height()), 2, 2)
        p.setPen(MUTED)
        p.setFont(QFont(BODY_FONT, 8))
        p.drawText(QRectF(0, 302, self.width(), 18), Qt.AlignHCenter, f"{self.status}, version {self.version}")
