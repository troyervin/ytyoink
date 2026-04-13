"""Configuration management for YTYoink — load, save, migrate ytdl_config.json."""

import json
import os
from dataclasses import dataclass, field, asdict


DEFAULTS = {
    "DownloadFolder": None,
    "Format": "m4a",
    "ShowProgress": False,
    "CoverSource": "itunes",
    "MetadataSource": "itunes",
    "ChooseCover": False,
    "OpenAfterDownload": False,
    "TurboMode": False,
}


@dataclass
class AppConfig:
    download_folder: str | None = None
    format: str = "m4a"
    show_progress: bool = False
    cover_source: str = "itunes"  # "itunes" or "youtube"
    metadata_source: str = "itunes"  # "itunes" or "youtube"
    open_after_download: bool = False
    turbo_mode: bool = False

    _path: str = field(default="", repr=False)

    @classmethod
    def load(cls, path: str) -> "AppConfig":
        cfg = cls(_path=path)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        else:
            data = {}

        cfg._migrate(data)

        cfg.download_folder = data.get("DownloadFolder") or None
        cfg.format = (data.get("Format") or "m4a").lower()
        if cfg.format not in ("m4a", "mp3"):
            cfg.format = "m4a"

        cfg.show_progress = bool(data.get("ShowProgress", False))

        cs = (data.get("CoverSource") or "itunes").lower()
        if cs not in ("itunes", "youtube", "none"):
            cs = "itunes"  # migrate "ask" → "itunes"
        cfg.cover_source = cs

        ms = (data.get("MetadataSource") or "itunes").lower()
        if ms not in ("itunes", "youtube"):
            ms = "itunes"
        cfg.metadata_source = ms

        cfg.open_after_download = bool(data.get("OpenAfterDownload", False))
        cfg.turbo_mode = bool(data.get("TurboMode", False))

        return cfg

    @staticmethod
    def _migrate(data: dict) -> None:
        """Backward-compat migration from older config schemas."""
        # ChooseThumb -> ChooseCover
        if "ChooseThumb" in data and "ChooseCover" not in data:
            data["ChooseCover"] = bool(data["ChooseThumb"])

        # ChooseCover -> CoverSource
        if "CoverSource" not in data and "ChooseCover" in data:
            data["CoverSource"] = "ask" if data["ChooseCover"] else "youtube"

        # Ensure all keys exist
        for key, default in DEFAULTS.items():
            if key not in data:
                data[key] = default

    def save(self) -> None:
        data = {
            "DownloadFolder": self.download_folder,
            "Format": self.format,
            "ShowProgress": self.show_progress,
            "CoverSource": self.cover_source,
            "MetadataSource": self.metadata_source,
            "OpenAfterDownload": self.open_after_download,
            "TurboMode": self.turbo_mode,
            "ChooseCover": False,  # kept for backward compat with PS script
        }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass  # non-fatal — continue with in-memory config
