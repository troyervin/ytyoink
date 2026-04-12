"""Dependency auto-install and auto-update — port of Ensure-Dependency and Ensure-YtDlpUpdated."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import winreg


CREATE_NO_WINDOW = 0x08000000


def _refresh_path() -> None:
    """Re-read PATH from the Windows registry so newly installed tools are found."""
    try:
        machine_key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        )
        machine_path, _ = winreg.QueryValueEx(machine_key, "Path")
        winreg.CloseKey(machine_key)
    except OSError:
        machine_path = ""

    try:
        user_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
        user_path, _ = winreg.QueryValueEx(user_key, "Path")
        winreg.CloseKey(user_key)
    except OSError:
        user_path = ""

    new_path = f"{machine_path};{user_path}"

    # Also include WinGet shim dir
    winget_links = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
    if os.path.isdir(winget_links) and winget_links not in new_path:
        new_path += f";{winget_links}"

    os.environ["PATH"] = new_path


def _find_command(name: str) -> bool:
    """Check if a command is available, including WinGet shims and standalone install dirs."""
    if shutil.which(name):
        return True

    local = os.environ.get("LOCALAPPDATA", "")

    # Check WinGet shim directory
    winget_links = os.path.join(local, "Microsoft", "WinGet", "Links")
    exe_path = os.path.join(winget_links, f"{name}.exe")
    if os.path.isfile(exe_path):
        if winget_links not in os.environ.get("PATH", ""):
            os.environ["PATH"] += f";{winget_links}"
        return True

    # Check standalone install directories used by _install_*_standalone
    standalone_dir = os.path.join(local, name)  # e.g. %LOCALAPPDATA%\ffmpeg or \yt-dlp
    exe_path = os.path.join(standalone_dir, f"{name}.exe")
    if os.path.isfile(exe_path):
        if standalone_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] += f";{standalone_dir}"
        return True

    return False


def _get_ssl_context():
    """Build an SSL context using certifi bundle (works in frozen PyInstaller exe)."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download_file(url: str, dest: str, status_callback=None, label: str = "") -> bool:
    """Download a file with progress reporting."""
    import urllib.request

    if status_callback:
        status_callback(f"Downloading {label}...")

    try:
        ctx = _get_ssl_context()
        req = urllib.request.Request(url, headers={"User-Agent": "YTYoink/1.0"})
        resp = urllib.request.urlopen(req, timeout=300, context=ctx)
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 256 * 1024  # 256KB chunks
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if status_callback and total:
                    pct = int(downloaded * 100 / total)
                    status_callback(f"Downloading {label}... {pct}%")
        return True
    except Exception as e:
        if status_callback:
            status_callback(f"Download failed ({label}): {e}")
        # Clean up partial download
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError:
            pass
        return False


def _install_ytdlp_standalone(status_callback=None) -> bool:
    """Download yt-dlp.exe directly from GitHub releases."""
    ytdlp_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "yt-dlp")
    os.makedirs(ytdlp_dir, exist_ok=True)

    ytdlp_exe = os.path.join(ytdlp_dir, "yt-dlp.exe")
    if os.path.isfile(ytdlp_exe):
        if ytdlp_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] += f";{ytdlp_dir}"
        return True

    url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    if not _download_file(url, ytdlp_exe, status_callback, "yt-dlp"):
        return False

    if ytdlp_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] += f";{ytdlp_dir}"

    return os.path.isfile(ytdlp_exe)


def _install_ffmpeg_standalone(status_callback=None) -> bool:
    """Download ffmpeg from gyan.dev essentials build and extract to a local folder."""
    import io
    import zipfile

    ffmpeg_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ffmpeg")
    os.makedirs(ffmpeg_dir, exist_ok=True)

    # Check if we already extracted it previously
    ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe")
    if os.path.isfile(ffmpeg_exe):
        if ffmpeg_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] += f";{ffmpeg_dir}"
        return True

    zip_path = os.path.join(ffmpeg_dir, "ffmpeg.zip")
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    if not _download_file(url, zip_path, status_callback, "ffmpeg (~80MB)"):
        return False

    if status_callback:
        status_callback("Extracting ffmpeg...")

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                basename = os.path.basename(member)
                if basename in ("ffmpeg.exe", "ffprobe.exe"):
                    content = zf.read(member)
                    dest = os.path.join(ffmpeg_dir, basename)
                    with open(dest, "wb") as f:
                        f.write(content)
    except Exception:
        return False
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass

    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] += f";{ffmpeg_dir}"

    return os.path.isfile(ffmpeg_exe)


def ensure_dependency(name: str, winget_id: str, status_callback=None) -> bool:
    """Ensure a tool is installed. Tries winget, then pip/standalone download.

    Returns True if the tool is available, False if installation failed.
    """
    if _find_command(name):
        if status_callback:
            status_callback(f"{name} ready.")
        return True

    # Try winget first
    if shutil.which("winget"):
        if status_callback:
            status_callback(f"Installing missing dependency: {name}...")
        try:
            subprocess.run(
                [
                    "winget", "install",
                    "--id", winget_id,
                    "-e", "--silent",
                    "--disable-interactivity",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                ],
                creationflags=CREATE_NO_WINDOW,
                capture_output=True,
                timeout=300,
            )
        except Exception:
            pass
        _refresh_path()
        if _find_command(name):
            if status_callback:
                status_callback(f"{name} installed successfully.")
            return True
        # Winget installed but binary not on PATH yet — fall through to standalone

    # Fallback: standalone download (also covers winget-installed-but-not-on-PATH)
    if name == "yt-dlp":
        if _install_ytdlp_standalone(status_callback):
            if status_callback:
                status_callback("yt-dlp ready.")
            return True
    elif name == "ffmpeg":
        if _install_ffmpeg_standalone(status_callback):
            if status_callback:
                status_callback("ffmpeg ready.")
            return True

    if status_callback:
        status_callback(f"{name} is required but could not be installed.")
    return False


def update_ytdlp(status_callback=None) -> str | None:
    """Auto-update yt-dlp. Returns the version string after update, or None on failure."""
    if not _find_command("yt-dlp"):
        return None

    if status_callback:
        status_callback("Checking for yt-dlp updates...")

    # Get current version
    ver_before = None
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        ver_before = result.stdout.strip()
    except Exception:
        pass

    # Try self-update
    needs_winget = True
    try:
        result = subprocess.run(
            ["yt-dlp", "-U"],
            capture_output=True, text=True, timeout=60,
            creationflags=CREATE_NO_WINDOW,
        )
        output = result.stdout + result.stderr
        if re.search(r"up to date|updated", output, re.IGNORECASE):
            needs_winget = False
        if re.search(r"distribution|package manager|cannot update", output, re.IGNORECASE):
            needs_winget = True
    except Exception:
        pass

    # Fallback to winget upgrade
    if needs_winget and shutil.which("winget"):
        try:
            subprocess.run(
                [
                    "winget", "upgrade",
                    "--id", "yt-dlp.yt-dlp",
                    "-e", "--silent",
                    "--disable-interactivity",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                ],
                creationflags=CREATE_NO_WINDOW,
                capture_output=True,
                timeout=120,
            )
        except Exception:
            pass

    # Get version after update
    ver_after = None
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        ver_after = result.stdout.strip()
    except Exception:
        pass

    if ver_after and status_callback:
        if ver_before and ver_after != ver_before:
            status_callback(f"yt-dlp version: {ver_after} (updated from {ver_before})")
        else:
            status_callback(f"yt-dlp version: {ver_after}")

    return ver_after


def _get_installed_dir() -> str | None:
    """Return the install location if YTYoink is already installed, else None.

    Checks registry first, then falls back to common install locations so that
    pre-registry installs (before v1.1.4) are still detected.
    """
    # 1. Registry (v1.1.4+)
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\YTYoink",
        )
        install_dir, _ = winreg.QueryValueEx(key, "InstallLocation")
        winreg.CloseKey(key)
        if install_dir and os.path.isfile(os.path.join(install_dir, "YTYoink.exe")):
            return install_dir
    except Exception:
        pass

    # 2. Filesystem fallback — common install locations
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "YTYoink"),
        os.path.join(os.environ.get("ProgramW6432", r"C:\Program Files"), "YTYoink"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "YTYoink"),
        os.path.join(os.environ.get("APPDATA", ""), "YTYoink"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "YTYoink.exe")):
            return candidate

    return None


def run_uninstall() -> None:
    """Standalone uninstall UI — launched via `YTYoink.exe --uninstall` from Add/Remove Programs."""
    import tkinter as tk

    install_dir = _get_installed_dir()

    root = tk.Tk()
    root.title("YTYoink Uninstaller")
    root.resizable(False, False)
    root.configure(bg="#1e1e1e")
    root.geometry("440x160")

    frame = tk.Frame(root, bg="#1e1e1e")
    frame.pack(fill="both", expand=True, padx=24, pady=20)

    if not install_dir:
        tk.Label(frame, text="YTYoink does not appear to be installed.",
                 font=("Segoe UI Semibold", 10), bg="#1e1e1e", fg="#cdd6f4").pack(anchor="w")
        tk.Button(frame, text="Close", font=("Segoe UI", 10),
                  bg="#45475a", fg="#cdd6f4", activebackground="#585b70",
                  activeforeground="#cdd6f4", relief="flat", bd=0, padx=16, pady=4,
                  command=root.destroy).pack(anchor="w", pady=(16, 0))
        root.mainloop()
        return

    tk.Label(frame, text="Uninstall YTYoink?",
             font=("Segoe UI Semibold", 10), bg="#1e1e1e", fg="#cdd6f4").pack(anchor="w")
    tk.Label(frame, text=install_dir,
             font=("Segoe UI", 9), bg="#1e1e1e", fg="#6c7086").pack(anchor="w", pady=(2, 14))

    btn_row = tk.Frame(frame, bg="#1e1e1e")
    btn_row.pack(anchor="w")

    status_lbl = tk.Label(frame, text="",
                          font=("Segoe UI", 9), bg="#1e1e1e", fg="#a6adc8")

    def set_status(msg):
        root.after(0, status_lbl.config, {"text": msg})

    def _do_uninstall():
        import threading
        import time

        for w in btn_row.winfo_children():
            root.after(0, w.config, {"state": "disabled"})
        root.after(0, status_lbl.pack, {"anchor": "w", "pady": (10, 0)})
        root.after(0, root.geometry, "440x195")
        set_status("Stopping YTYoink...")

        def worker():
            # Exclude our own PID so we don't kill the uninstall process itself
            subprocess.run(
                ["taskkill", "/f", "/im", "YTYoink.exe",
                 "/fi", f"PID ne {os.getpid()}"],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )
            time.sleep(1)

            set_status("Removing shortcuts...")
            desktop_lnk = os.path.join(
                os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop", "YTYoink.lnk"
            )
            startmenu_dir = os.path.join(
                os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                "Microsoft", "Windows", "Start Menu", "Programs", "YTYoink"
            )
            try:
                os.remove(desktop_lnk)
            except OSError:
                pass
            shutil.rmtree(startmenu_dir, ignore_errors=True)

            set_status("Removing from Programs list...")
            subprocess.run(
                ["reg", "delete",
                 "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\YTYoink",
                 "/f"],
                capture_output=True, creationflags=CREATE_NO_WINDOW,
            )

            set_status("Removing files...")
            shutil.rmtree(install_dir, ignore_errors=True)

            set_status("Done. YTYoink has been removed.")
            root.after(2500, root.destroy)

        threading.Thread(target=worker, daemon=True).start()

    tk.Button(btn_row, text="Uninstall", font=("Segoe UI Semibold", 10),
              bg="#f38ba8", fg="#1e1e2e", activebackground="#f5a3b8",
              activeforeground="#1e1e2e", relief="flat", bd=0, padx=16, pady=4,
              command=_do_uninstall).pack(side="left", padx=(0, 8))

    tk.Button(btn_row, text="Cancel", font=("Segoe UI", 10),
              bg="#45475a", fg="#cdd6f4", activebackground="#585b70",
              activeforeground="#cdd6f4", relief="flat", bd=0, padx=16, pady=4,
              command=root.destroy).pack(side="left")

    root.mainloop()


def bootstrap_install() -> None:
    """First-run setup: runs when _internal/ is missing (onefile bootstrap exe).

    If already installed, offers Reinstall / Uninstall / Cancel.
    Otherwise shows an install-location picker, downloads YTYoink_full.zip,
    extracts the full onedir installation to the chosen folder, then launches
    YTYoink.exe from there.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog
    import urllib.request
    import zipfile
    from version import GITHUB_REPO, APP_VERSION

    already_installed = _get_installed_dir()
    default_install = already_installed or os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"), "YTYoink"
    )
    zip_url = f"https://github.com/{GITHUB_REPO}/releases/latest/download/YTYoink_full.zip"
    zip_path = os.path.join(tempfile.gettempdir(), "YTYoink_full.zip")
    extract_dir = os.path.join(tempfile.gettempdir(), "YTYoink_setup")

    root = tk.Tk()
    root.title("YTYoink Setup")
    root.resizable(False, False)
    root.configure(bg="#1e1e1e")

    # ── Phase 0: already-installed prompt (shown only when installed) ──────────
    if already_installed:
        root.geometry("440x145")
        exist_frame = tk.Frame(root, bg="#1e1e1e")
        exist_frame.pack(fill="both", expand=True, padx=24, pady=20)

        tk.Label(exist_frame, text="YTYoink is already installed.",
                 font=("Segoe UI Semibold", 10), bg="#1e1e1e", fg="#cdd6f4").pack(anchor="w")
        tk.Label(exist_frame, text=already_installed,
                 font=("Segoe UI", 9), bg="#1e1e1e", fg="#6c7086").pack(anchor="w", pady=(2, 18))

        btn_row = tk.Frame(exist_frame, bg="#1e1e1e")
        btn_row.pack(anchor="w")

        def _go_reinstall():
            exist_frame.pack_forget()
            root.geometry("440x185")
            pick_frame.pack(fill="both", expand=True, padx=20, pady=14)

        def _go_uninstall():
            for w in btn_row.winfo_children():
                w.config(state="disabled")

            status_lbl = tk.Label(exist_frame, text="Stopping YTYoink...",
                                  font=("Segoe UI", 9), bg="#1e1e1e", fg="#a6adc8")
            status_lbl.pack(anchor="w", pady=(10, 0))
            root.geometry("440x185")

            def set_status(msg):
                root.after(0, status_lbl.config, {"text": msg})

            def do_uninstall():
                import time

                # Kill running app
                subprocess.run(["taskkill", "/f", "/im", "YTYoink.exe"],
                               capture_output=True, creationflags=CREATE_NO_WINDOW)
                time.sleep(1)

                # Remove shortcuts
                set_status("Removing shortcuts...")
                desktop_lnk = os.path.join(
                    os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop", "YTYoink.lnk"
                )
                startmenu_dir = os.path.join(
                    os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                    "Microsoft", "Windows", "Start Menu", "Programs", "YTYoink"
                )
                try:
                    os.remove(desktop_lnk)
                except OSError:
                    pass
                shutil.rmtree(startmenu_dir, ignore_errors=True)

                # Remove registry entry
                set_status("Removing from Programs list...")
                subprocess.run(
                    ["reg", "delete",
                     "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\YTYoink",
                     "/f"],
                    capture_output=True, creationflags=CREATE_NO_WINDOW,
                )

                # Remove install folder
                set_status("Removing files...")
                shutil.rmtree(already_installed, ignore_errors=True)

                set_status("Done. YTYoink has been removed.")
                root.after(2500, root.destroy)

            import threading
            threading.Thread(target=do_uninstall, daemon=True).start()

        tk.Button(btn_row, text="Reinstall", font=("Segoe UI Semibold", 10),
                  bg="#89b4fa", fg="#1e1e2e", activebackground="#b4d0fb",
                  activeforeground="#1e1e2e", relief="flat", bd=0, padx=16, pady=4,
                  command=_go_reinstall).pack(side="left", padx=(0, 8))

        tk.Button(btn_row, text="Uninstall", font=("Segoe UI Semibold", 10),
                  bg="#f38ba8", fg="#1e1e2e", activebackground="#f5a3b8",
                  activeforeground="#1e1e2e", relief="flat", bd=0, padx=16, pady=4,
                  command=_go_uninstall).pack(side="left", padx=(0, 8))

        tk.Button(btn_row, text="Cancel", font=("Segoe UI", 10),
                  bg="#45475a", fg="#cdd6f4", activebackground="#585b70",
                  activeforeground="#cdd6f4", relief="flat", bd=0, padx=16, pady=4,
                  command=root.destroy).pack(side="left")

    # ── Phase 1: install location picker ──────────────────────────────────────
    pick_frame = tk.Frame(root, bg="#1e1e1e")
    if not already_installed:
        root.geometry("440x185")
        pick_frame.pack(fill="both", expand=True, padx=20, pady=14)

    tk.Label(pick_frame, text="Install location:", font=("Segoe UI", 10),
             bg="#1e1e1e", fg="#a6adc8").pack(anchor="w")

    path_row = tk.Frame(pick_frame, bg="#1e1e1e")
    path_row.pack(fill="x", pady=(4, 10))

    path_var = tk.StringVar(value=default_install)
    path_entry = tk.Entry(path_row, textvariable=path_var, font=("Segoe UI", 9),
                          bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                          relief="flat", bd=0)
    path_entry.pack(side="left", fill="x", expand=True, ipady=4)

    def browse():
        parent = os.path.dirname(path_var.get()) or "C:\\"
        chosen = filedialog.askdirectory(title="Choose install folder", initialdir=parent)
        if chosen:
            chosen = chosen.replace("/", "\\")
            if not chosen.lower().endswith("\\ytyoink"):
                chosen = os.path.join(chosen, "YTYoink")
            path_var.set(chosen)

    tk.Button(path_row, text="Browse", font=("Segoe UI", 9),
              bg="#45475a", fg="#cdd6f4", activebackground="#585b70",
              activeforeground="#cdd6f4", relief="flat", bd=0, padx=10,
              command=browse).pack(side="right", padx=(8, 0), ipady=4)

    shortcut_row = tk.Frame(pick_frame, bg="#1e1e1e")
    shortcut_row.pack(anchor="w", pady=(0, 10))

    desktop_var = tk.BooleanVar(value=True)
    startmenu_var = tk.BooleanVar(value=True)

    tk.Checkbutton(shortcut_row, text="Desktop shortcut", variable=desktop_var,
                   font=("Segoe UI", 9), bg="#1e1e1e", fg="#a6adc8",
                   activebackground="#1e1e1e", activeforeground="#cdd6f4",
                   selectcolor="#313244", highlightthickness=0, bd=0).pack(side="left", padx=(0, 16))

    tk.Checkbutton(shortcut_row, text="Start Menu shortcut", variable=startmenu_var,
                   font=("Segoe UI", 9), bg="#1e1e1e", fg="#a6adc8",
                   activebackground="#1e1e1e", activeforeground="#cdd6f4",
                   selectcolor="#313244", highlightthickness=0, bd=0).pack(side="left")

    install_btn = tk.Button(pick_frame, text="  Install  ", font=("Segoe UI Semibold", 10),
                            bg="#89b4fa", fg="#1e1e2e", activebackground="#b4d0fb",
                            activeforeground="#1e1e2e", relief="flat", bd=0, padx=20, pady=4,
                            command=lambda: _start_install())
    install_btn.pack()

    # ── Phase 2: progress ─────────────────────────────────────────────────────
    prog_frame = tk.Frame(root, bg="#1e1e1e")

    lbl = tk.Label(prog_frame, text="", font=("Segoe UI", 10),
                   bg="#1e1e1e", fg="#cccccc")
    lbl.pack(pady=(22, 8))
    bar = ttk.Progressbar(prog_frame, mode="indeterminate", length=380)
    bar.pack()

    def set_status(text):
        root.after(0, lbl.config, {"text": text})

    def _start_install():
        install_dir = path_var.get().strip()
        if not install_dir:
            return
        want_desktop = desktop_var.get()
        want_startmenu = startmenu_var.get()
        install_btn.config(state="disabled")
        pick_frame.pack_forget()
        root.geometry("440x100")
        prog_frame.pack(fill="both", expand=True, padx=20)
        bar.start(10)
        import threading
        threading.Thread(
            target=lambda: _run_setup(install_dir, want_desktop, want_startmenu),
            daemon=True,
        ).start()

    def _run_setup(install_dir, want_desktop, want_startmenu):
        target_exe = os.path.join(install_dir, "YTYoink.exe")
        try:
            set_status("Downloading YTYoink...")
            ctx = _get_ssl_context()
            req = urllib.request.Request(zip_url, headers={"User-Agent": "YTYoink/1.0"})
            with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(zip_path, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(downloaded * 100 / total)
                            set_status(f"Downloading YTYoink... {pct}%")

            # Unblock the zip so Defender doesn't flag extracted files
            try:
                subprocess.run(
                    ["powershell", "-NonInteractive", "-Command",
                     f"Unblock-File -Path '{zip_path}'"],
                    creationflags=CREATE_NO_WINDOW, capture_output=True, timeout=10,
                )
            except Exception:
                pass

            set_status("Extracting...")
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir, ignore_errors=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)

            # Unblock the extracted exe
            extracted_exe = os.path.join(extract_dir, "YTYoink", "YTYoink.exe")
            try:
                subprocess.run(
                    ["powershell", "-NonInteractive", "-Command",
                     f"Unblock-File -Path '{extracted_exe}'"],
                    creationflags=CREATE_NO_WINDOW, capture_output=True, timeout=10,
                )
            except Exception:
                pass

            set_status("Installing...")

            src = os.path.join(extract_dir, "YTYoink")
            bat_path = os.path.join(tempfile.gettempdir(), "_ytyoink_setup.bat")

            # Paths used by shortcuts
            desktop_lnk = os.path.join(
                os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop", "YTYoink.lnk"
            )
            startmenu_dir = os.path.join(
                os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                "Microsoft", "Windows", "Start Menu", "Programs", "YTYoink"
            )
            startmenu_lnk = os.path.join(startmenu_dir, "YTYoink.lnk")

            # ── Build PowerShell shortcut commands ────────────────────────────────
            ps_shortcuts = []
            if want_desktop:
                ps_shortcuts.append(
                    f"$ws=New-Object -ComObject WScript.Shell;"
                    f"$lnk=$ws.CreateShortcut('{desktop_lnk}');"
                    f"$lnk.TargetPath='{target_exe}';"
                    f"$lnk.WorkingDirectory='{install_dir}';"
                    f"$lnk.IconLocation='{target_exe},0';"
                    f"$lnk.Save()"
                )
            if want_startmenu:
                ps_shortcuts.append(
                    f"New-Item -ItemType Directory -Force -Path '{startmenu_dir}' | Out-Null;"
                    f"$ws=New-Object -ComObject WScript.Shell;"
                    f"$lnk=$ws.CreateShortcut('{startmenu_lnk}');"
                    f"$lnk.TargetPath='{target_exe}';"
                    f"$lnk.WorkingDirectory='{install_dir}';"
                    f"$lnk.IconLocation='{target_exe},0';"
                    f"$lnk.Save()"
                )

            shortcut_lines = ""
            if ps_shortcuts:
                ps_cmd = "; ".join(ps_shortcuts)
                shortcut_lines = (
                    "echo Creating shortcuts...\r\n"
                    f'powershell -NonInteractive -Command "{ps_cmd}"\r\n'
                )

            # ── Write Add/Remove Programs registry entry directly from Python ────
            # (avoids cmd/PowerShell quoting hell for paths with spaces)
            set_status("Registering...")
            reg_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\YTYoink"
            try:
                key = winreg.CreateKeyEx(
                    winreg.HKEY_LOCAL_MACHINE, reg_path,
                    0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
                )
                winreg.SetValueEx(key, "DisplayName",    0, winreg.REG_SZ,    "YTYoink")
                winreg.SetValueEx(key, "UninstallString",0, winreg.REG_SZ,    f'"{target_exe}" --uninstall')
                winreg.SetValueEx(key, "DisplayIcon",    0, winreg.REG_SZ,    f"{target_exe},0")
                winreg.SetValueEx(key, "Publisher",      0, winreg.REG_SZ,    "YTYoink")
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ,    APP_VERSION)
                winreg.SetValueEx(key, "InstallLocation",0, winreg.REG_SZ,    install_dir)
                winreg.SetValueEx(key, "NoModify",       0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair",       0, winreg.REG_DWORD, 1)
                winreg.CloseKey(key)
            except Exception as reg_err:
                set_status(f"Warning: registry write failed ({reg_err})")
                import time; time.sleep(2)

            bat = (
                "@echo off\r\n"
                "title YTYoink Setup\r\n"
                "echo Installing YTYoink...\r\n"
                "timeout /t 3 /nobreak >nul\r\n"
                f'robocopy "{src}" "{install_dir}" /e /is /it /np /nfl /ndl\r\n'
                "set RC=%errorlevel%\r\n"
                "if %RC% GEQ 8 (\r\n"
                "    echo Installation failed. Please try again.\r\n"
                "    timeout /t 5 /nobreak >nul\r\n"
                "    goto :end\r\n"
                ")\r\n"
                + shortcut_lines
                + "echo Launching YTYoink...\r\n"
                "timeout /t 1 /nobreak >nul\r\n"
                f'powershell -WindowStyle Hidden -Command "Start-Process \'{target_exe}\'"\r\n'
                ":end\r\n"
                f'rd /s /q "{extract_dir}" 2>nul\r\n'
                f'del "{zip_path}" 2>nul\r\n'
                'del "%~f0"\r\n'
            )
            with open(bat_path, "w") as f:
                f.write(bat)

            subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                close_fds=True,
            )
            root.after(0, root.destroy)

        except Exception as e:
            set_status(f"Setup failed: {e}")
            root.after(0, bar.stop)

    root.mainloop()


def update_self(github_repo: str, current_version: str, status_callback=None) -> bool:
    """Check GitHub for a newer YTYoink release and silently apply it.

    Only runs when frozen (packaged exe). Returns True if an update was
    downloaded — the caller should exit so the replacement batch can run.
    Never raises — every failure path returns False silently.
    """
    if not getattr(sys, "frozen", False):
        return False  # Skip when running as plain Python script

    # Step 1: fetch latest release from GitHub API — silent on any error
    try:
        import urllib.request
        api_url = f"https://api.github.com/repos/{github_repo}/releases/latest"
        ctx = _get_ssl_context()
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": "YTYoink/1.0", "Accept": "application/vnd.github+json"},
        )
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        data = json.loads(resp.read().decode())
        latest = str(data.get("tag_name", "")).strip().lstrip("v")
    except Exception:
        return False

    if not latest:
        return False

    # Step 2: compare versions
    def _ver(v):
        try:
            return tuple(int(x) for x in re.split(r"[.\-]", v.lstrip("v"))[:3])
        except Exception:
            return (0,)

    if _ver(latest) <= _ver(current_version):
        return False  # Already up to date — no noise

    if status_callback:
        status_callback(f"YTYoink {latest} available — downloading update...")

    # Step 3: download new exe — _download_file already handles errors and cleans up
    exe_url = f"https://github.com/{github_repo}/releases/latest/download/YTYoink.exe"
    exe_dir = os.path.dirname(sys.executable)
    new_exe = os.path.join(exe_dir, "YTYoink_new.exe")

    if not _download_file(exe_url, new_exe, status_callback, f"YTYoink {latest}"):
        return False

    # Strip the "Mark of the Web" so Defender doesn't treat it as an internet download
    try:
        subprocess.run(
            ["powershell", "-NonInteractive", "-Command",
             f"Unblock-File -Path '{new_exe}'"],
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass  # non-fatal — update still attempted

    if status_callback:
        status_callback("Update downloaded — restarting...")

    # Step 4: write replacement batch — clean up new_exe on failure
    current_exe = sys.executable
    bat_path = os.path.join(tempfile.gettempdir(), "_ytyoink_update.bat")
    try:
        bat = (
            "@echo off\r\n"
            "title YTYoink Updater\r\n"
            "echo.\r\n"
            "echo  YTYoink Update\r\n"
            "echo  Waiting for app to close...\r\n"
            "timeout /t 6 /nobreak >nul\r\n"
            f'if not exist "{new_exe}" (\r\n'
            "    echo  Update file missing - skipped.\r\n"
            "    timeout /t 3 /nobreak >nul\r\n"
            "    goto :end\r\n"
            ")\r\n"
            "echo  Applying update...\r\n"
            f'move /y "{new_exe}" "{current_exe}" >nul\r\n'
            "if errorlevel 1 (\r\n"
            "    echo  Could not apply update - skipped.\r\n"
            f'    del "{new_exe}" 2>nul\r\n'
            "    timeout /t 3 /nobreak >nul\r\n"
            "    goto :end\r\n"
            ")\r\n"
            "echo  Done! Restarting YTYoink...\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'powershell -WindowStyle Hidden -Command "Start-Process \'{current_exe}\'"\r\n'
            ":end\r\n"
            'del "%~f0"\r\n'
        )
        with open(bat_path, "w") as f:
            f.write(bat)
    except Exception:
        try:
            os.remove(new_exe)
        except OSError:
            pass
        return False

    # Step 5: launch the batch with its own console so the user can see progress
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", bat_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    except Exception:
        try:
            os.remove(new_exe)
            os.remove(bat_path)
        except OSError:
            pass
        return False

    return True  # Caller should close the app
