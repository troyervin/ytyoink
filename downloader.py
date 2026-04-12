"""Core download pipeline — yt-dlp + ffmpeg orchestration.

Ports Invoke-Downloader from the PS script into a structured class with
progress reporting, cancellation, and clean separation from the GUI.
"""

import json
import msvcrt
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Event
from typing import Callable

from config import AppConfig
from itunes import ItunesMatch, search_itunes
from metadata import ParsedFilename, clean_title, parse_filename
from url_utils import normalize_youtube_url

CREATE_NO_WINDOW = 0x08000000
TEMP_BASE = os.path.join(tempfile.gettempdir(), "ytmp-dlp")
AUDIO_EXTENSIONS = {".m4a", ".webm", ".opus", ".mp3", ".mka", ".aac", ".flac", ".wav"}
THUMB_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png"}


def _is_folder_locked(folder: str) -> bool:
    """Check if a run folder's lock file is held by another process."""
    lock_path = os.path.join(folder, ".lock")
    if not os.path.isfile(lock_path):
        return False  # no lock file = not locked
    try:
        fh = open(lock_path, "r+")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            # Got the lock — folder is NOT actively locked by another process
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            fh.close()
            return False
        except OSError:
            # Couldn't acquire = another process holds it
            fh.close()
            return True
    except Exception:
        return False  # can't open file — treat as unlocked (stale)


def cleanup_stale_temp() -> int:
    """Remove stale run folders on startup. Skips folders locked by other instances.

    Uses a 2-hour cutoff (more aggressive than the per-download 1-day prune).
    Also removes empty run folders regardless of age.
    Returns the number of folders removed.
    """
    removed = 0
    try:
        if not os.path.isdir(TEMP_BASE):
            return 0
        cutoff = time.time() - 7200  # 2 hours
        for entry in os.scandir(TEMP_BASE):
            if not (entry.is_dir() and entry.name.startswith("run_")):
                continue
            try:
                # Skip if another instance is actively using it
                if _is_folder_locked(entry.path):
                    continue
                # Remove if old enough or empty
                contents = list(os.scandir(entry.path))
                is_empty = len(contents) == 0 or (
                    len(contents) == 1 and contents[0].name == ".lock"
                )
                if is_empty or entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


class CancelledError(Exception):
    pass


@dataclass
class VideoInfo:
    title: str = ""
    uploader: str = ""
    duration: int = 0
    thumbnail_url: str | None = None
    release_year: str | None = None
    release_date: str | None = None
    release_timestamp: int | None = None
    genre: str | None = None
    album: str | None = None
    raw_json: dict = field(default_factory=dict, repr=False)


@dataclass
class DownloadResult:
    output_path: str
    filename: str
    metadata: dict
    cover_source_used: str


def sanitize_filename(name: str) -> str:
    """Remove characters invalid in Windows filenames."""
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    name = name.rstrip(". ")
    return name if name else "audio"


def get_unique_path(folder: str, basename: str, ext: str) -> str:
    """Get a file path that doesn't collide with existing files."""
    path = os.path.join(folder, f"{basename}.{ext}")
    i = 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{basename} ({i}).{ext}")
        i += 1
    return path


class DownloadPipeline:
    def __init__(
        self,
        config: AppConfig,
        status_callback: Callable[[str], None] | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
    ):
        self.config = config
        self.status_callback = status_callback or (lambda msg: None)
        self.progress_callback = progress_callback or (lambda pct, msg: None)
        self._cancel = Event()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise CancelledError("Download cancelled by user")

    def _status(self, msg: str):
        self.status_callback(msg)

    def _progress(self, pct: float, msg: str):
        self.progress_callback(pct, msg)

    def cancel(self):
        self._cancel.set()

    def reset_cancel(self):
        self._cancel.clear()

    # ---- Fetch video info (no download) ----

    def fetch_video_info(self, url: str) -> VideoInfo:
        """Run yt-dlp --skip-download --print-json to get video metadata."""
        self._check_cancel()

        result = subprocess.run(
            ["yt-dlp", "--no-playlist", "--skip-download", "--print-json", url],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() or "yt-dlp failed to fetch video info"
            raise RuntimeError(error_msg)

        # yt-dlp may print multiple JSON objects; take the last valid one
        raw = None
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue

        if not raw:
            raise RuntimeError("No valid JSON from yt-dlp")

        info = VideoInfo(
            title=raw.get("title", "") or raw.get("fulltitle", ""),
            uploader=raw.get("uploader", "") or raw.get("channel", ""),
            duration=int(raw.get("duration", 0) or 0),
            thumbnail_url=raw.get("thumbnail"),
            release_year=str(raw["release_year"]) if raw.get("release_year") else None,
            release_date=raw.get("release_date"),
            release_timestamp=raw.get("release_timestamp"),
            genre=raw.get("genre"),
            album=raw.get("album"),
            raw_json=raw,
        )
        return info

    # ---- Full download pipeline ----

    def download(
        self,
        url: str,
        video_info: VideoInfo | None,
        metadata_overrides: dict,
        cover_choice: str,
        itunes_match: ItunesMatch | None,
        custom_cover_path: str | None = None,
    ) -> DownloadResult:
        """Execute the full download, encode, and embed pipeline.

        Args:
            url: Normalized YouTube URL.
            video_info: Pre-fetched VideoInfo (or None to fetch here).
            metadata_overrides: Dict of field->value for user overrides.
            cover_choice: "youtube", "itunes", "none", or "custom".
            itunes_match: Pre-fetched ItunesMatch (or None to search here).
            custom_cover_path: Filesystem path to custom cover art image.

        Returns:
            DownloadResult with output path and metadata used.
        """
        self.reset_cancel()

        # Ensure temp base exists
        os.makedirs(TEMP_BASE, exist_ok=True)

        # Prune stale run folders (>1 day old)
        self._prune_old_runs()

        # Create unique run-temp folder with lock file
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        rand = uuid.uuid4().hex[:8]
        run_temp = os.path.join(TEMP_BASE, f"run_{stamp}_{rand}")
        os.makedirs(run_temp, exist_ok=True)

        lock_fh = None
        try:
            # Hold an exclusive lock so other instances won't prune this folder
            lock_path = os.path.join(run_temp, ".lock")
            lock_fh = open(lock_path, "w")
            try:
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                pass  # couldn't lock — continue anyway

            return self._do_download(url, video_info, metadata_overrides, cover_choice, itunes_match, run_temp, custom_cover_path)
        except CancelledError:
            self._status("Cancelled.")
            raise
        finally:
            # Release lock and cleanup run temp folder
            try:
                if lock_fh:
                    lock_fh.close()
            except Exception:
                pass
            try:
                if os.path.isdir(run_temp):
                    shutil.rmtree(run_temp, ignore_errors=True)
            except Exception:
                pass

    def _do_download(
        self,
        url: str,
        video_info: VideoInfo | None,
        metadata_overrides: dict,
        cover_choice: str,
        itunes_match: ItunesMatch | None,
        run_temp: str,
        custom_cover_path: str | None = None,
    ) -> DownloadResult:
        self._check_cancel()
        self._status("Downloading audio...")

        # Marker file for exact filepath capture
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        rand = uuid.uuid4().hex[:8]
        path_file = os.path.join(run_temp, f"_last_download_{stamp}_{rand}.txt")

        # Build yt-dlp args
        dl_args = [
            "yt-dlp",
            "--no-playlist",
            "--no-part",
            "--no-mtime",
            "--write-thumbnail",
            "--print-to-file", "after_move:filepath", path_file,
            "-f", "bestaudio",
            "-o", os.path.join(run_temp, "%(artist)s - %(title)s.%(ext)s"),
            url,
        ]

        # Run yt-dlp
        process = subprocess.Popen(
            dl_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )

        output_lines = []
        try:
            for line in process.stdout:
                line = line.rstrip()
                output_lines.append(line)

                # Parse progress
                if "[download]" in line and "%" in line:
                    m = re.search(r"(\d+\.?\d*)%", line)
                    if m:
                        pct = float(m.group(1))
                        self._progress(pct * 0.5, line)  # 0-50% for download phase
                else:
                    self._status(line)

                if self._cancel.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise CancelledError("Download cancelled by user")
        finally:
            process.wait()

        if process.returncode != 0:
            raise RuntimeError(
                f"yt-dlp failed (exit code {process.returncode}). "
                + "\n".join(output_lines[-5:])
            )

        time.sleep(0.2)

        # Find downloaded audio file
        raw_audio = self._find_downloaded_audio(path_file, run_temp)
        if not raw_audio:
            raise RuntimeError(f"No downloaded audio file found in {run_temp}")

        self._check_cancel()

        raw_name = os.path.basename(raw_audio)

        # Find thumbnail
        base_no_ext = os.path.splitext(os.path.basename(raw_audio))[0]
        thumb_pick = self._find_thumbnail(run_temp, base_no_ext)

        # Fetch video info if not provided
        if not video_info:
            self._status("Fetching video info...")
            try:
                video_info = self.fetch_video_info(url)
            except Exception:
                video_info = VideoInfo()

        # Parse filename and build metadata
        self._status("Processing metadata...")
        self._progress(55, "Processing metadata...")

        meta = parse_filename(raw_name)

        # Enrich from video_info
        if not meta.artist or meta.artist in ("NA", "Unknown Artist"):
            if video_info.uploader:
                meta = ParsedFilename(
                    label=meta.label,
                    artist=video_info.uploader,
                    featuring=meta.featuring,
                    title_raw=meta.title_raw,
                )

        tag_title = clean_title(meta.title_raw, meta.featuring)
        tag_artist = meta.artist
        tag_album = meta.label if meta.label and meta.label != "NA" else ""
        tag_year = None
        tag_genre = None

        # Enrich album from video info
        if not tag_album and video_info.album:
            tag_album = video_info.album

        # Year from video info
        if video_info.release_year:
            tag_year = str(video_info.release_year)
        elif video_info.release_date and len(video_info.release_date) >= 4:
            tag_year = video_info.release_date[:4]
        elif video_info.release_timestamp:
            try:
                dt = datetime.utcfromtimestamp(int(video_info.release_timestamp))
                tag_year = str(dt.year)
            except Exception:
                pass

        # Genre from video info
        if video_info.genre:
            tag_genre = video_info.genre

        # iTunes enrichment — always query to get proper album/year/genre
        # since YouTube often fills these with channel names
        self._check_cancel()

        best_it = itunes_match
        is_swap = False

        if not best_it:
            self._status("Querying iTunes...")
            self._progress(60, "Querying iTunes...")
            best_it, is_swap = self._query_itunes(tag_artist, tag_title)

        if best_it:
            if is_swap:
                tag_artist = best_it.artist
                tag_title = best_it.song
            else:
                if not tag_artist or tag_artist in ("NA", "Unknown Artist"):
                    tag_artist = best_it.artist
                if not tag_title:
                    tag_title = best_it.song

            # Always prefer iTunes for album/year/genre when available
            if best_it.album:
                tag_album = best_it.album
            if best_it.year:
                tag_year = best_it.year
            if best_it.genre:
                tag_genre = best_it.genre

        # Apply user overrides
        if "title" in metadata_overrides:
            tag_title = metadata_overrides["title"]
        if "artist" in metadata_overrides:
            tag_artist = metadata_overrides["artist"]
        if "album" in metadata_overrides:
            tag_album = metadata_overrides["album"]
        if "year" in metadata_overrides:
            tag_year = metadata_overrides["year"]
        if "genre" in metadata_overrides:
            tag_genre = metadata_overrides["genre"]

        # Cover art
        self._check_cancel()
        self._status("Processing cover art...")
        self._progress(70, "Processing cover art...")

        png_path = os.path.join(run_temp, f"{base_no_ext}.png")
        cover_source_used = self._process_cover(
            cover_choice, thumb_pick, best_it, run_temp, png_path, custom_cover_path
        )

        # Build output filename from final tags
        self._check_cancel()
        final_ext = "mp3" if self.config.format == "mp3" else "m4a"

        if tag_artist and tag_title:
            base_name = f"{tag_artist} - {tag_title}"
        else:
            base_name = "audio"
        base_name = sanitize_filename(base_name)

        final_path = get_unique_path(self.config.download_folder, base_name, final_ext)

        # Encode with ffmpeg
        self._check_cancel()
        self._status("Embedding metadata...")
        self._progress(80, "Embedding metadata...")

        has_cover = os.path.isfile(png_path)
        self._encode(raw_audio, png_path, has_cover, final_ext, final_path, {
            "artist": tag_artist or "",
            "title": tag_title or "",
            "album": tag_album or "",
            "date": tag_year or "",
            "genre": tag_genre or "",
            "comment": "",
        })

        safe_file = os.path.basename(final_path)
        self._status(f"Saved: {safe_file}")
        self._progress(100, f"Saved: {safe_file}")

        return DownloadResult(
            output_path=final_path,
            filename=safe_file,
            metadata={
                "title": tag_title,
                "artist": tag_artist,
                "album": tag_album,
                "year": tag_year,
                "genre": tag_genre,
            },
            cover_source_used=cover_source_used,
        )

    def _find_downloaded_audio(self, path_file: str, run_temp: str) -> str | None:
        """Find the downloaded audio file, preferring the yt-dlp path marker."""
        # Try path file first
        if os.path.isfile(path_file):
            try:
                with open(path_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if lines:
                    printed = lines[-1].strip().strip('"')
                    if printed and os.path.isfile(printed):
                        return printed
            except Exception:
                pass

        # Fallback: newest audio file in run_temp
        candidates = []
        try:
            for entry in os.scandir(run_temp):
                if entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in AUDIO_EXTENSIONS:
                        candidates.append(entry)
        except Exception:
            pass

        if candidates:
            candidates.sort(key=lambda e: e.stat().st_mtime, reverse=True)
            return candidates[0].path

        return None

    def _find_thumbnail(self, run_temp: str, base_name: str) -> str | None:
        """Find the thumbnail file downloaded by yt-dlp."""
        try:
            for entry in os.scandir(run_temp):
                if entry.is_file():
                    name_no_ext = os.path.splitext(entry.name)[0]
                    ext = os.path.splitext(entry.name)[1].lower()
                    if name_no_ext == base_name and ext in THUMB_EXTENSIONS:
                        return entry.path
        except Exception:
            pass
        return None

    def _query_itunes(self, artist: str, title: str) -> tuple[ItunesMatch | None, bool]:
        """Query iTunes with both orderings. Returns (match, is_swapped)."""
        it1 = None
        it2 = None

        if artist and title:
            it1 = search_itunes(artist, title)
            it2 = search_itunes(title, artist)
        elif title:
            it1 = search_itunes("", title)
        elif artist:
            it1 = search_itunes(artist, "")

        best = it1
        is_swap = False
        if it2 and (not it1 or it2.score > (it1.score + 2)):
            best = it2
            is_swap = True

        return best, is_swap

    def _process_cover(
        self,
        cover_choice: str,
        thumb_pick: str | None,
        itunes_match: ItunesMatch | None,
        run_temp: str,
        png_path: str,
        custom_cover_path: str | None = None,
    ) -> str:
        """Download and crop cover art. Returns the source used."""
        import requests

        cover_file = thumb_pick
        source_used = "youtube"

        if cover_choice == "none":
            return "none"

        if cover_choice == "custom" and custom_cover_path and os.path.isfile(custom_cover_path):
            cover_file = custom_cover_path
            source_used = "custom"
            # Fall through to ffmpeg crop-to-square below
        elif cover_choice == "itunes":
            # Try iTunes artwork, fall back to YouTube
            if itunes_match and itunes_match.artwork_url:
                itunes_cover = os.path.join(run_temp, "itunes.jpg")
                try:
                    resp = requests.get(itunes_match.artwork_url, timeout=12)
                    resp.raise_for_status()
                    with open(itunes_cover, "wb") as f:
                        f.write(resp.content)
                    cover_file = itunes_cover
                    source_used = "itunes"
                except Exception:
                    pass  # keep YouTube thumb
        elif cover_choice == "youtube":
            pass  # use thumb_pick as-is
        else:
            # cover_choice could be "itunes" or "youtube" from "ask" mode selection
            if cover_choice == "itunes" and itunes_match and itunes_match.artwork_url:
                itunes_cover = os.path.join(run_temp, "itunes.jpg")
                try:
                    resp = requests.get(itunes_match.artwork_url, timeout=12)
                    resp.raise_for_status()
                    with open(itunes_cover, "wb") as f:
                        f.write(resp.content)
                    cover_file = itunes_cover
                    source_used = "itunes"
                except Exception:
                    pass

        # Rare fallback: no YouTube thumb, try iTunes
        if not cover_file and itunes_match and itunes_match.artwork_url:
            itunes_cover = os.path.join(run_temp, "itunes.jpg")
            try:
                resp = requests.get(itunes_match.artwork_url, timeout=12)
                resp.raise_for_status()
                with open(itunes_cover, "wb") as f:
                    f.write(resp.content)
                cover_file = itunes_cover
                source_used = "itunes"
            except Exception:
                pass

        # Crop to square PNG
        if cover_file and os.path.isfile(cover_file):
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-v", "quiet",
                        "-i", cover_file,
                        "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
                        "-frames:v", "1", "-update", "1",
                        png_path,
                    ],
                    creationflags=CREATE_NO_WINDOW,
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass

        return source_used

    def _encode(
        self,
        raw_audio: str,
        png_path: str,
        has_cover: bool,
        fmt: str,
        output_path: str,
        tags: dict,
    ) -> None:
        """Encode the final audio file with embedded metadata and cover art."""
        ff_args = ["ffmpeg", "-y", "-v", "quiet", "-i", raw_audio]

        if has_cover:
            ff_args += ["-i", png_path]

        if fmt == "mp3":
            ff_args += ["-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "0"]
            if has_cover:
                ff_args += [
                    "-map", "1:v:0",
                    "-c:v", "mjpeg",
                    "-id3v2_version", "3",
                    "-metadata:s:v", "title=Album cover",
                    "-metadata:s:v", "comment=Cover (front)",
                    "-disposition:v:0", "attached_pic",
                ]
        else:
            ff_args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "256k"]
            if has_cover:
                ff_args += [
                    "-map", "1:v:0",
                    "-c:v", "mjpeg",
                    "-disposition:v:0", "attached_pic",
                ]

        for key, value in tags.items():
            if value or key == "comment":
                ff_args += ["-metadata", f"{key}={value}"]

        ff_args.append(output_path)

        result = subprocess.run(
            ff_args,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")

    def _prune_old_runs(self) -> None:
        """Remove stale run folders older than 1 day, skipping locked ones."""
        try:
            cutoff = time.time() - 86400  # 1 day
            for entry in os.scandir(TEMP_BASE):
                if entry.is_dir() and entry.name.startswith("run_"):
                    try:
                        if entry.stat().st_mtime < cutoff:
                            if _is_folder_locked(entry.path):
                                continue  # another instance is using it
                            shutil.rmtree(entry.path, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass

    # ---- Cover art download for preview (used by GUI) ----

    def download_cover_bytes(self, url: str) -> bytes | None:
        """Download an image URL and return raw bytes."""
        import requests
        try:
            resp = requests.get(url, timeout=12)
            resp.raise_for_status()
            return resp.content
        except Exception:
            return None

    def download_youtube_thumb(self, video_info: VideoInfo) -> bytes | None:
        """Download YouTube thumbnail for preview."""
        if video_info.thumbnail_url:
            return self.download_cover_bytes(video_info.thumbnail_url)
        return None

    def download_itunes_cover(self, itunes_match: ItunesMatch) -> bytes | None:
        """Download iTunes artwork for preview."""
        if itunes_match and itunes_match.artwork_url:
            return self.download_cover_bytes(itunes_match.artwork_url)
        return None
