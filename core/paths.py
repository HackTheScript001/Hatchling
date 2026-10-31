"""Where Hatchling keeps things.

Built-in definition files ship inside the app (read-only once installed in Program Files).
Your own files go in Documents\\Hatchling\\<kind> and are loaded alongside them - a file there
with the same name as a built-in one replaces it.
"""
import os
import sys

APP_NAME = "Hatchling"


def app_dir() -> str:
    """Folder holding the bundled data (PyInstaller unpack dir when frozen)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_dir(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", APP_NAME, *parts)


def asset(name: str) -> str:
    return os.path.join(app_dir(), "assets", name)


def definition_files(kind: str):
    """(filename, full path) for every .json of a kind, user files overriding built-ins by name."""
    found = {}
    for folder in (os.path.join(app_dir(), kind), user_dir(kind)):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if fn.endswith(".json") and not fn.startswith("_"):
                found[fn] = os.path.join(folder, fn)
    return sorted(found.items())


def ensure_user_dirs():
    """Creates Documents\\Hatchling\\... and copies each _TEMPLATE.json there so it's easy to find."""
    import shutil
    for kind in ("features", "services", "livedata", "vehicles", "backups", "logs", "reports"):
        os.makedirs(user_dir(kind), exist_ok=True)
        src = os.path.join(app_dir(), kind, "_TEMPLATE.json")
        dst = user_dir(kind, "_TEMPLATE.json")
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copyfile(src, dst)
            except OSError:
                pass
