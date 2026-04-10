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

    if status_callback:
        status_callback("Update downloaded — restarting...")

    # Step 4: write replacement batch — clean up new_exe on failure
    current_exe = sys.executable
    bat_path = os.path.join(tempfile.gettempdir(), "_ytyoink_update.bat")
    try:
        bat = (
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'move /y "{new_exe}" "{current_exe}"\r\n'
            f'start "" "{current_exe}"\r\n'
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

    # Step 5: launch the batch detached — clean up both files on failure
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", bat_path],
            creationflags=subprocess.DETACHED_PROCESS | CREATE_NO_WINDOW,
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
