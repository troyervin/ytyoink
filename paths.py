"""Path resolution for both normal Python and PyInstaller frozen modes.

Two directories matter:
  asset_dir  — where bundled files live (images, icons)
               Frozen: sys._MEIPASS  |  Normal: script directory
  app_dir    — where user data lives (config JSON, placed beside the exe)
               Frozen: directory containing the .exe  |  Normal: script directory
"""

import os
import sys


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def asset_dir() -> str:
    """Directory containing bundled assets (logo PNGs, .ico)."""
    if _is_frozen():
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def app_dir() -> str:
    """Directory for user data (config file).  Lives beside the .exe when frozen."""
    if _is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))
