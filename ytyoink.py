"""YTYoink — YouTube Audio Downloader GUI.

Entry point: bootstrap detection, dependency checks, config loading, and app launch.
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

    # Uninstall mode — launched by Add/Remove Programs via UninstallString.
    if "--uninstall" in sys.argv:
        from dependencies import run_uninstall
        run_uninstall()
        return

    # Bootstrap check: if this is YTYoink_setup.exe (by name) OR _internal/ is
    # missing, we are the onefile setup exe — run installer and exit.
    if getattr(sys, "frozen", False):
        exe_name = os.path.basename(sys.executable).lower()
        exe_dir = os.path.dirname(sys.executable)
        is_setup = (exe_name == "ytyoink_setup.exe" or
                    not os.path.isdir(os.path.join(exe_dir, "_internal")))
        if is_setup:
            from dependencies import bootstrap_install
            bootstrap_install()
            return

    import tkinter as tk
    from paths import app_dir

    config_path = os.path.join(app_dir(), "ytdl_config.json")

    from config import AppConfig
    config = AppConfig.load(config_path)

    from gui.app import YTYoinkApp
    app = YTYoinkApp(config)

    app.after(100, app.prompt_download_folder)
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
