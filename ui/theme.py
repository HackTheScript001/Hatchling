"""Hatchling's look: a workshop service sheet.

Light steel page, white panels, graphite ink. Indicator amber is reserved for the main
action on each screen; green means on/OK, red means danger, blue marks links and highlights.
Headings, the car's name and gauge numbers use Bahnschrift (DIN-style, built into
Windows 10/11); everything else uses Segoe UI.

A dark variant exists for night use in the garage - same roles, different values.
"""

LIGHT = {
    "PAGE": "#E4E8EC", "PANEL": "#FFFFFF", "PANEL_ALT": "#F3F5F7", "INK": "#1E242B", "MUTED": "#5A6672",
    "FAINT": "#8C96A0", "LINE": "#CBD3DA", "LINE_STRONG": "#AEB8C1", "SIDEBAR": "#1E242B",
    "SIDEBAR_INK": "#C7CED5", "SIDEBAR_HEAD": "#7F8A95", "SIDEBAR_SEL": "#2C343D",
    "ACCENT": "#235A8C", "AMBER": "#F0A202", "AMBER_INK": "#2A1E00", "AMBER_HOVER": "#FFB41F",
    "OK": "#2F7D4F", "WARN": "#A86400", "DANGER": "#B3261E", "CHANGED": "#FFE6A8", "SELECT": "#D6E4F2",
    "LOG_BG": "#F3F5F7", "LOG_INK": "#46525E",
}

DARK = {
    "PAGE": "#1B1F24", "PANEL": "#252B32", "PANEL_ALT": "#20252B", "INK": "#E9ECEF", "MUTED": "#A3ADB7",
    "FAINT": "#76808A", "LINE": "#39424C", "LINE_STRONG": "#4A545F", "SIDEBAR": "#14171B",
    "SIDEBAR_INK": "#C7CED5", "SIDEBAR_HEAD": "#7F8A95", "SIDEBAR_SEL": "#262C33",
    "ACCENT": "#7FB3E3", "AMBER": "#F0A202", "AMBER_INK": "#2A1E00", "AMBER_HOVER": "#FFB41F",
    "OK": "#5FBF85", "WARN": "#F0B54A", "DANGER": "#F07167", "CHANGED": "#5C4512", "SELECT": "#2F4A66",
    "LOG_BG": "#1F2429", "LOG_INK": "#A9B4BE",
}

T = dict(LIGHT)
# module-level names the app imports (kept in sync by apply())
ACCENT = OK = WARN = DANGER = AMBER = INK = MUTED = FAINT = LINE = LINE_STRONG = PANEL = PANEL_ALT = CHANGED = ""

HEAD_FONT = "Bahnschrift"
BODY_FONT = "Segoe UI"


def apply(dark: bool = False):
    T.clear()
    T.update(DARK if dark else LIGHT)
    g = globals()
    for k in ("ACCENT", "OK", "WARN", "DANGER", "AMBER", "INK", "MUTED", "FAINT", "LINE", "LINE_STRONG",
              "PANEL", "PANEL_ALT", "CHANGED"):
        g[k] = T[k]
    g["STYLE"] = style()


def style() -> str:
    t = T
    return f"""
* {{ font-family: '{BODY_FONT}', sans-serif; font-size: 10pt; color: {t['INK']}; }}
QMainWindow, QWidget#page, QStackedWidget, QDialog {{ background: {t['PAGE']}; }}
QToolTip {{ background: {t['INK']}; color: {t['PAGE']}; border: none; padding: 5px 8px; }}

QListWidget#nav {{ background: {t['SIDEBAR']}; border: none; padding: 6px 0; outline: 0; }}
QListWidget#nav::item {{ padding: 8px 18px; color: {t['SIDEBAR_INK']}; border-left: 4px solid transparent; }}
QListWidget#nav::item:hover {{ background: {t['SIDEBAR_SEL']}; }}
QListWidget#nav::item:selected {{ background: {t['SIDEBAR_SEL']}; color: #FFFFFF; border-left: 4px solid {t['AMBER']}; }}
QListWidget#nav::item:disabled {{ color: {t['SIDEBAR_HEAD']}; padding: 16px 18px 4px 18px; }}

QLabel#title {{ font-family: '{HEAD_FONT}'; font-size: 20pt; font-weight: 600; }}
QLabel#carname {{ font-family: '{HEAD_FONT}'; font-size: 30pt; font-weight: 600; }}
QLabel#subtle {{ color: {t['MUTED']}; }}
QLabel#figure {{ font-family: '{HEAD_FONT}'; font-size: 15pt; }}

QFrame#card {{ background: {t['PANEL']}; border: 1px solid {t['LINE']}; border-radius: 4px; }}
QFrame#plain {{ background: transparent; border: none; border-top: 1px solid {t['LINE']}; }}

QPushButton {{ background: {t['PANEL']}; border: 1px solid {t['LINE_STRONG']}; border-radius: 3px;
               padding: 6px 14px; }}
QPushButton:hover {{ border-color: {t['INK']}; }}
QPushButton:focus {{ border: 2px solid {t['ACCENT']}; }}
QPushButton:disabled {{ color: {t['FAINT']}; border-color: {t['LINE']}; }}
QPushButton#primary {{ background: {t['AMBER']}; color: {t['AMBER_INK']}; border: 1px solid #C98500; font-weight: 600; }}
QPushButton#primary:hover {{ background: {t['AMBER_HOVER']}; }}
QPushButton#danger {{ background: {t['PANEL']}; color: {t['DANGER']}; border: 1px solid {t['DANGER']}; font-weight: 600; }}
QPushButton#task {{ text-align: left; padding: 14px 16px; font-size: 11pt; background: {t['PANEL']};
                    border: 1px solid {t['LINE']}; border-radius: 4px; }}
QPushButton#task:hover {{ border-color: {t['AMBER']}; }}
QPushButton#link {{ border: none; background: transparent; color: {t['ACCENT']}; padding: 2px 0; text-align: left; }}
QPushButton#link:hover {{ text-decoration: underline; }}

QComboBox, QLineEdit, QPlainTextEdit {{ background: {t['PANEL']}; border: 1px solid {t['LINE_STRONG']};
               border-radius: 3px; padding: 5px 8px; selection-background-color: {t['SELECT']}; }}
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus {{ border: 2px solid {t['ACCENT']}; }}
QComboBox QAbstractItemView {{ background: {t['PANEL']}; selection-background-color: {t['SELECT']};
               selection-color: {t['INK']}; }}

QTableWidget, QListWidget {{ background: {t['PANEL']}; gridline-color: {t['LINE']}; border: 1px solid {t['LINE']};
               border-radius: 3px; selection-background-color: {t['SELECT']}; selection-color: {t['INK']};
               alternate-background-color: {t['PANEL_ALT']}; }}
QListWidget::item {{ padding: 6px 8px; }}
QHeaderView::section {{ background: {t['PANEL_ALT']}; border: none; border-bottom: 1px solid {t['LINE']};
               padding: 6px; color: {t['MUTED']}; font-weight: 600; }}
QTextBrowser {{ background: {t['PANEL']}; border: 1px solid {t['LINE']}; border-radius: 3px; padding: 6px; }}

QPlainTextEdit#log {{ background: {t['LOG_BG']}; border: none; border-top: 1px solid {t['LINE']}; border-radius: 0;
               font-family: Consolas, monospace; font-size: 9pt; color: {t['LOG_INK']}; }}
QProgressBar {{ background: {t['LINE']}; border: none; border-radius: 2px; max-height: 6px; }}
QProgressBar::chunk {{ background: {t['ACCENT']}; border-radius: 2px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
QStatusBar {{ background: {t['PANEL']}; border-top: 1px solid {t['LINE']}; color: {t['MUTED']}; }}
QStatusBar QLabel {{ color: {t['MUTED']}; padding: 0 6px; }}
QSplitter::handle {{ background: {t['PAGE']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {t['LINE_STRONG']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


STYLE = ""
apply(False)
