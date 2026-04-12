# YTYoink — Project Reference

> Pass this file at the start of any new Claude session working on this project.

---

## What It Is

YTYoink is a Windows desktop app (tkinter GUI, PyInstaller frozen exe) that downloads audio from YouTube, enriches metadata via the iTunes API, embeds cover art, and produces clean M4A/MP3 files ready for a music library. iTunes is the primary source of truth for album, year, and genre.

**Current version:** see `version.py` → `APP_VERSION`
**GitHub repo:** `troyervin/ytyoink` (public, no description, no README — intentionally invisible)

---

## File Structure

```
YTYoink/
  ytyoink.py          Entry point — DPI awareness, bootstrap detection, config load, app launch
  version.py          APP_VERSION + GITHUB_REPO — only file that needs bumping before release
  config.py           AppConfig dataclass — loads/saves/migrates ytdl_config.json
  dependencies.py     Dependency install/update + self-updater + uninstaller
  downloader.py       DownloadPipeline — yt-dlp → metadata → iTunes → cover → ffmpeg
  itunes.py           iTunes Search API — scoring, dual-ordering (artist+title and title+artist)
  metadata.py         parse_filename(), clean_title() — strips [Official Video] etc.
  url_utils.py        normalize_youtube_url() — youtu.be, /shorts/, playlist → watch?v=
  paths.py            asset_dir(), app_dir() — frozen vs. dev path resolution
  build.py            PyInstaller onedir build script (called by release.py)
  release.py          Full release pipeline — bump, build, tag, GitHub release
  PROJECT.md          This file
  gui/
    app.py            YTYoinkApp(tk.Tk) — layout, events, threading orchestration
    widgets.py        CheckboxEntry, ImagePreview, StatusBar
    styles.py         Dark theme constants (colors, fonts, sizes)
  dist/               Build output (gitignored) — do not commit
  backups/            Local exe snapshots per release (gitignored)
```

---

## Release Process — ALWAYS use release.py

**This is the single most important thing to know:**

```bash
python release.py 1.1.21
```

**Do not** manually commit version changes and push. **Do not** push commits to GitHub separately. The release script handles everything.

### What `release.py` does (7 steps):

1. Bumps `APP_VERSION` in `version.py` to the given version
2. Builds `dist/YTYoink/YTYoink.exe` (onedir, via `build.py` / PyInstaller)
3. Builds `dist/YTYoink_setup.exe` (onefile bootstrap for first-time install)
4. Zips the full onedir folder to `dist/YTYoink_full.zip` (downloaded by setup on first run)
5. Copies both exes to `backups/` as versioned snapshots
6. `git tag v1.1.21` and `git push origin v1.1.21` (tag only — commits stay local)
7. `gh release create v1.1.21` with all three assets attached

### Three release assets and what each does:

| Asset | Purpose |
|-------|---------|
| `YTYoink.exe` | Downloaded by the **auto-updater** on subsequent updates |
| `YTYoink_setup.exe` | Share with friends for **first-time install** (self-bootstrapping onefile) |
| `YTYoink_full.zip` | Downloaded by setup on **first run** to extract the full onedir app |

### Git workflow quirk

`.gitignore` uses `*` (ignore everything) + `!.gitignore`. All tracked files need `-f`:

```bash
git add -f gui/app.py dependencies.py version.py   # etc.
git commit -m "v1.1.21: description"
python release.py 1.1.21
```

Commits accumulate locally and are **not pushed to origin/main** — only tags get pushed. This is intentional: the repo has no source code, just release assets.

---

## Auto-Update System

On every launch, a background thread:

1. Calls `dependencies.update_self(GITHUB_REPO, APP_VERSION, cb)` 
2. Fetches `https://api.github.com/repos/troyervin/ytyoink/releases/latest`
3. Compares tag version against bundled `APP_VERSION` using tuple comparison
4. If newer: downloads `YTYoink.exe` from the release to `<install_dir>/YTYoink_new.exe`
5. Runs `Unblock-File` (strips Mark of the Web so Defender allows it)
6. Updates registry `DisplayVersion` and `UninstallString` in HKLM
7. Writes `%TEMP%\_ytyoink_update.bat` — launched detached with its own console
8. App closes after 1.5 seconds; batch runs, kills old PID, swaps exe, relaunches

**Status messages shown:**
- "Checking for updates..." (before check)
- "YTYoink v1.1.20 — up to date." (if current)
- "YTYoink 1.1.21 available — downloading update..." (if newer found)
- "Update downloaded — restarting..." (after download)

Every failure path is silent and non-fatal — network errors, bad JSON, etc. all fall through to normal startup.

### Startup sequence (in order):

1. Stale temp cleanup (`downloader.cleanup_stale_temp`)
2. **Self-update check** (`update_self`) — exits early and restarts if update applied
3. `ensure_dependency("ffmpeg", ...)` — winget → gyan.dev zip fallback
4. `ensure_dependency("yt-dlp", ...)` — winget → GitHub exe fallback
5. `update_ytdlp()` — `yt-dlp -U` → winget fallback
6. Sets `_deps_ready = True`, enables Fetch button, shows "Ready."

Status bar filters: messages ending in `" ready."` are suppressed (ffmpeg/yt-dlp noise). Download progress (`%` suffix) updates the last line in-place via `update_last`.

---

## Install / Uninstall System

### First install (via `YTYoink_setup.exe`)
- Detects if already installed (`HKLM\...\Uninstall\YTYoink\InstallLocation`)
- Shows install path picker (defaults to existing location or `C:\Program Files\YTYoink`)
- Downloads `YTYoink_full.zip` from latest GitHub release
- Extracts to install dir, creates Start Menu shortcut and Desktop shortcut
- Writes registry keys: `DisplayName`, `DisplayVersion`, `InstallLocation`, `UninstallString`
- `UninstallString` format: `"C:\Program Files\YTYoink\YTYoink.exe" --uninstall`

### Uninstall (via `--uninstall` flag)
- Launched by Windows Settings/Add & Remove Programs via the `UninstallString`
- `ytyoink.py` detects `--uninstall` in `sys.argv` BEFORE the bootstrap check
- Calls `dependencies.run_uninstall()` — standalone tkinter UI (no main app needed)
- Steps: `taskkill /f /im YTYoink.exe /fi "PID ne {os.getpid()}"`, remove shortcuts, remove registry, remove install dir

### Registry path
`HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\YTYoink`

Always write with `KEY_WOW64_64KEY` flag to reach the 64-bit hive from an elevated process.

---

## GUI Architecture

### Canvas + scrollbar layout

The main window uses a `tk.Canvas` with an embedded `tk.Frame` (`_main_frame`) to support vertical scrolling:

```
YTYoinkApp (tk.Tk)
  └── canvas_frame (Frame)
        ├── _canvas (Canvas) — fill="both", expand=True
        │     └── _main_frame (Frame, embedded via create_window)
        │           └── [all content widgets]
        └── _scrollbar — packed/unpacked dynamically
```

Key behaviors:
- `_on_frame_configure` — updates `scrollregion` when content height changes
- `_on_canvas_configure` — fires on window resize; stretches `_canvas_window` to `max(natural_h, canvas_h)` so the status bar fills space
- `_update_scrollbar_visibility` — uses `winfo_reqheight()` (natural height) not bbox height, so scrollbar only appears when content actually overflows
- `_sync_canvas()` — call this (via `self.after(0, self._sync_canvas)`) after dynamically packing/unpacking large content blocks. Combines scrollregion + height stretch + visibility in one shot. Required because `_on_canvas_configure` only fires on window resize, not content changes.

### Threading rule

All network/subprocess work runs on daemon threads. GUI updates **must** go through `self.after(0, callback, args)`. Never touch tkinter widgets from a background thread.

### StatusBar — `append` vs `update_last`

- `append(text, tag)` — adds a new line. Detects if previous line lacks a trailing `\n` (left by `update_last`) and inserts one first to prevent concatenation.
- `update_last(text, tag)` — replaces the last line in-place (used for download progress %).

---

## Key Behaviors and Gotchas

**Clipboard auto-paste on URL focus:** When the URL entry gets focus, `_on_url_focus` fires → defers 50ms → `_check_clipboard_for_url` reads clipboard and checks if it's a YouTube URL (`youtube.com` or `youtu.be`). If it's new (differs from current field content), pastes and triggers fetch.

**Last download label:** Clickable (cursor="hand2"). On click, opens Explorer with the file selected (`subprocess.run(["explorer", "/select,", path])`). Color: FG_ACCENT at rest, FG_TEXT on hover.

**Min window height:** Set dynamically after first render via `_enforce_min_height` → `winfo_reqheight()` on `_main_frame`. Never hardcoded.

**Temp dir:** `os.path.join(tempfile.gettempdir(), "ytmp-dlp")` — not hardcoded to `C:\temp`.

**UAC / elevation:** Exe has `--uac-admin` PyInstaller flag → `requireAdministrator` manifest. This is required for writing to `C:\Program Files` and HKLM registry. Child processes (batch scripts) inherit elevation — no extra UAC prompts.

**URL normalization:** Done in `_on_fetch_info` via `normalize_youtube_url()`. Playlist params stripped. User sees the normalized URL in the entry field.

**Metadata source toggle:** Shown only when iTunes match found. User can switch between iTunes and YouTube metadata mid-session. Preference saved to config.

**`keep_overrides` checkbox:** When checked, field overrides survive a new fetch (don't reset on next URL). When unchecked, fields reset to auto-detected values each fetch.

---

## Settings (`ytdl_config.json`)

Stored beside the exe at `app_dir()`. Created on first run. Backward-compatible with original PowerShell script config format.

| Key | Values | Default |
|-----|--------|---------|
| `DownloadFolder` | path string | prompt on first run |
| `Format` | `"m4a"` / `"mp3"` | `"m4a"` |
| `CoverSource` | `"itunes"` / `"youtube"` / `"none"` | `"itunes"` |
| `MetadataSource` | `"itunes"` / `"youtube"` | `"itunes"` |
| `OpenAfterDownload` | bool | `false` |

---

## Build Details

PyInstaller **onedir** mode (not onefile). Onefile extracts to `%TEMP%` on every launch which causes Defender to rescan `python314.dll` each time — unacceptable startup delay.

Hidden imports required: `charset_normalizer`, `windnd`, `version` (dynamic import not detected by PyInstaller).

The `YTYoink.exe` bootloader + embedded PKG (which contains the PYZ with all Python bytecodes including `version.py`) are in the single exe file. The `_internal/` folder contains DLLs, Tcl/Tk files, and data assets — not Python source.

---

## GitHub Repo Rules

- **No README, no description, no topics, no release notes** — repo is intentionally invisible
- **No source code** — `.gitignore` is `*` (everything ignored); only `.gitignore` itself is in the repo
- Release names are tag only (`v1.1.20`) with empty notes (`--notes ""`)
- The repo is public only because GitHub's releases API requires public repos for unauthenticated access
