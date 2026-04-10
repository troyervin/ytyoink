# YTYoink — YouTube Audio Downloader

## Project Aim

YTYoink downloads audio from YouTube videos, enriches metadata via the iTunes API, embeds cover art, and produces clean M4A or MP3 files ready for a music library. It prioritizes accurate metadata over speed — iTunes is the primary source of truth for album, year, and genre information.

## Architecture

### Entry Point
- `ytyoink.py` — Sets DPI awareness, loads config, launches the tkinter GUI, triggers dependency checks and yt-dlp auto-update on startup.

### Backend Modules

| File | Purpose |
|------|---------|
| `config.py` | `AppConfig` dataclass — loads/saves/migrates `ytdl_config.json`. Backward-compatible with the original PowerShell script's config format. |
| `url_utils.py` | `normalize_youtube_url()` — Converts youtu.be, /shorts/, and playlist URLs to standard `watch?v=` format. |
| `metadata.py` | `parse_filename()` — Splits yt-dlp filenames into label/artist/featuring/title. `clean_title()` — Strips `[Official Video]`, `(Lyrics)`, etc. `normalize_meta_text()` — Lowercase + strip punctuation for comparison. |
| `itunes.py` | `search_itunes()` — Queries the iTunes Search API, scores results (+6 exact match, +3 partial, -1 for singles, tiebreak by earliest year), tries both (artist, title) and (title, artist) orderings. |
| `dependencies.py` | `ensure_dependency()` — Auto-installs yt-dlp/ffmpeg via winget if missing, including WinGet shim path detection and PATH refresh via registry. `update_ytdlp()` — Runs `yt-dlp -U` with winget fallback. |
| `downloader.py` | `DownloadPipeline` — Orchestrates the full pipeline: yt-dlp download, metadata parsing, iTunes enrichment, cover art acquisition, ffmpeg encoding, temp file management. Supports cancellation via `threading.Event`. |

### GUI Layer (`gui/`)

| File | Purpose |
|------|---------|
| `styles.py` | Dark theme constants — colors, fonts, dimensions. |
| `widgets.py` | `CheckboxEntry` — Checkbox + label + text field; unchecked = auto value (disabled/dim), checked = user override (enabled). `ImagePreview` — Displays image bytes as a square-cropped Pillow thumbnail. `StatusBar` — Read-only text area with color-coded tags (info/success/warning/error). |
| `app.py` | `YTYoinkApp(tk.Tk)` — Main window. Manages layout, threading (all network/subprocess work on daemon threads, GUI updates via `root.after()`), UI state machine (idle/fetching/ready/downloading). |

## Features

### Download Pipeline
1. Paste YouTube URL (auto-fetches on paste, or press Enter / click Fetch Info)
2. yt-dlp downloads best audio stream + thumbnail to a unique temp folder
3. Filename parsed for artist/title/featuring
4. iTunes API queried for metadata enrichment (always preferred for album/year/genre)
5. Cover art selected (YouTube thumbnail or iTunes artwork)
6. ffmpeg encodes final file with embedded metadata + cover art
7. Output saved to download folder with collision avoidance (`(1)`, `(2)`, etc.)
8. Temp files cleaned up; stale run folders (>1 day) auto-pruned

### Output Formats
- **M4A**: AAC codec at 256 kbps (`-c:a aac -b:a 256k`)
- **MP3**: libmp3lame VBR quality 0 (`-c:a libmp3lame -q:a 0`), ID3v2.3 tags

### Metadata
- **Fields**: Title, Artist, Album, Year, Genre, Comment (cleared)
- **Source priority**: iTunes always preferred for album/year/genre. YouTube used for artist/title when iTunes unavailable. User overrides take final precedence.
- **Title cleaning**: Removes `[Official Music Video]`, `(Lyrics)`, `(4K)`, trailing `- Lyrics`, etc.
- **Featuring**: Parsed from `Artist feat. Guest` notation, appended to title

### Cover Art
- **YouTube**: Video thumbnail, auto-cropped to square via ffmpeg
- **iTunes**: Album artwork at 600px (fallback: 100px upscaled to 600px), cropped to square
- **Ask mode**: Shows both covers side-by-side with radio buttons to choose
- **iTunes mode**: Uses iTunes artwork, falls back to YouTube if unavailable
- **YouTube mode**: Always uses YouTube thumbnail

### Settings (persisted in `ytdl_config.json`)
- Download folder path
- Output format (m4a/mp3)
- Cover art source (ask/youtube/itunes)
- Open file after download (true/false)
- ShowProgress (legacy, kept for PS script compat)

### Dependency Management
- Checks for yt-dlp and ffmpeg on PATH at startup
- Auto-installs via `winget install` if missing (silent, non-interactive)
- Checks WinGet shim directory and refreshes PATH from Windows registry
- Auto-updates yt-dlp on every launch (`yt-dlp -U`, winget fallback)

### GUI Features
- Auto-fetch video info on URL paste
- Video preview (thumbnail, title, uploader, duration)
- Metadata override checkboxes (check to edit, uncheck for auto)
- Side-by-side cover art comparison in Ask mode
- Open Folder button beside Browse
- Open file after download checkbox
- Progress bar + color-coded status log
- Cancel button (terminates subprocess, cleans temp files)
- Last download display
- Auto-hide scrollbar (only appears when content exceeds window)
- Custom window icon (`ytyoink.ico`)

## URL Support
- `youtube.com/watch?v=ID`
- `youtu.be/ID`
- `youtube.com/shorts/ID`
- Playlist parameters (`&list=`) automatically stripped

## Dependencies
- **Python 3.12+** with tkinter
- **Pillow** — Image display and processing in GUI
- **requests** — iTunes API and artwork downloads
- **yt-dlp** — Audio download and metadata extraction (auto-installed via winget)
- **ffmpeg** — Audio encoding, cover art cropping, metadata embedding (auto-installed via winget)

## File Structure
```
YTYoink/
  ytyoink.py              # Entry point
  config.py               # Config load/save/migrate
  dependencies.py         # Auto-install + auto-update
  downloader.py           # Download pipeline
  itunes.py               # iTunes API search + scoring
  metadata.py             # Filename parsing + title cleaning
  url_utils.py            # URL normalization
  ytyoink.ico             # Window icon
  ytdl_config.json        # Persisted settings (generated at runtime)
  gui/
    __init__.py
    app.py                # Main window
    widgets.py            # Custom widgets
    styles.py             # Theme constants
  backup/                 # Snapshot of working state
  YoutubeDownloaderFull.ps1  # Original PowerShell script (reference)
  youtubemp3downloader.bat   # Original batch launcher
```

## Design Principles
- iTunes metadata is always preferred over YouTube for album, year, and genre
- All network/subprocess work runs on background threads; GUI never freezes
- Settings persist between sessions; backward-compatible with PS script config
- Temp files always cleaned up, even on errors or cancellation
- No console windows flash on Windows (CREATE_NO_WINDOW flag on all subprocesses)
- User overrides always take final precedence over any auto-detected values
