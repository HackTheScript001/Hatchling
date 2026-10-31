import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pick day/night colours before any screen code reads them
from core.library import settings  # noqa: E402
from ui import theme  # noqa: E402

try:
    theme.apply(bool(settings().get("dark_mode")))
except Exception:
    theme.apply(False)

from ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
