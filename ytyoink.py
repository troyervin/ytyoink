"""YTYoink — YouTube Audio Downloader GUI.

Entry point: dependency checks, config loading, and app launch.
"""

import os
import sys


def main():
    # DPI awareness must be set before any tkinter import
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    import tkinter as tk
    from paths import app_dir

    # Determine paths — config lives beside the exe (or script)
    config_path = os.path.join(app_dir(), "ytdl_config.json")

    # Load config
    from config import AppConfig
    config = AppConfig.load(config_path)

    # Create and launch the app
    from gui.app import YTYoinkApp
    app = YTYoinkApp(config)

    # First-run: prompt for download folder if needed
    app.after(100, app.prompt_download_folder)

    # Run dependency checks and yt-dlp update in background
    app.after(200, app.run_startup_tasks)

    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        crash_path = os.path.join(
            os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)),
            "crash.log",
        )
        with open(crash_path, "w") as f:
            traceback.print_exc(file=f)
        raise
