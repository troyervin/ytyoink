"""Build YTYoink into a standalone Windows .exe using PyInstaller.

Usage:
    python build.py              — single-file portable exe (default)
    python build.py --onedir     — folder build (faster startup, larger)

Produces:
    dist/YTYoink.exe             (default, single file)
    dist/YTYoink/YTYoink.exe    (--onedir mode)
"""

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY_POINT = os.path.join(SCRIPT_DIR, "ytyoink.py")
ICON_PATH = os.path.join(SCRIPT_DIR, "ytyoink.ico")

# Assets to bundle (source path → destination folder inside bundle)
DATA_FILES = [
    ("logo_24.png", "."),
    ("logo_32.png", "."),
    ("logo_48.png", "."),
    ("ytyoink.ico", "."),
]


def main():
    onedir = "--onedir" in sys.argv

    # Build --add-data args  (src;dst on Windows)
    add_data = []
    for src, dst in DATA_FILES:
        full = os.path.join(SCRIPT_DIR, src)
        if not os.path.isfile(full):
            print(f"  WARNING: asset not found: {full}")
            continue
        add_data.extend(["--add-data", f"{full};{dst}"])

    mode_flag = "--onedir" if onedir else "--onefile"
    mode_label = "folder" if onedir else "single-file"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        mode_flag,
        "--windowed",           # no console window
        "--noconfirm",          # overwrite dist/ without asking
        f"--icon={ICON_PATH}",
        "--name=YTYoink",
        "--hidden-import=charset_normalizer",
        "--hidden-import=windnd",
        "--hidden-import=version",
        *add_data,
        ENTRY_POINT,
    ]

    print(f"Building YTYoink ({mode_label} mode)...")
    print(f"  {' '.join(cmd)}\n")
    subprocess.run(cmd, cwd=SCRIPT_DIR, check=True)

    if onedir:
        exe_path = os.path.join(SCRIPT_DIR, "dist", "YTYoink", "YTYoink.exe")
    else:
        exe_path = os.path.join(SCRIPT_DIR, "dist", "YTYoink.exe")

    if os.path.isfile(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"\nBuild complete!  {exe_path}  ({size_mb:.1f} MB)")
        if onedir:
            folder = os.path.dirname(exe_path)
            total = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fns in os.walk(folder)
                for f in fns
            )
            print(f"  Folder total: {total / (1024 * 1024):.1f} MB")
    else:
        print("\nBuild finished but exe not found — check output above for errors.")


if __name__ == "__main__":
    main()
