"""Filename parsing, title cleaning, and text normalization.

Ports Parse-FileName, title-cleaning regexes, and Normalize-MetaText from the PS script.
"""

import os
import re
from dataclasses import dataclass


@dataclass
class ParsedFilename:
    label: str
    artist: str
    featuring: str | None
    title_raw: str


def parse_filename(filename: str) -> ParsedFilename:
    """Split a yt-dlp output filename into label, artist, featuring, title.

    Patterns handled:
      3 parts: "Label - Artist - Title"
      2 parts: "Artist - Title"
      1 part:  entire name becomes title, artist = "Unknown Artist"
    """
    name_no_ext = os.path.splitext(filename)[0]
    parts = name_no_ext.split(" - ")

    if len(parts) >= 3:
        label = parts[0].strip()
        artist_feat = parts[1]
        title_raw = " - ".join(parts[2:])
    elif len(parts) == 2:
        label = ""
        artist_feat = parts[0]
        title_raw = parts[1]
    else:
        label = ""
        artist_feat = "Unknown Artist"
        title_raw = name_no_ext

    # Extract "feat." from artist
    m = re.match(r"^(?P<artist>.+?)\s+feat\.?\s+(?P<feat>.+)$", artist_feat, re.IGNORECASE)
    if m:
        artist = m.group("artist").strip()
        featuring = m.group("feat").strip()
    else:
        artist = artist_feat.strip()
        featuring = None

    return ParsedFilename(
        label=label,
        artist=artist,
        featuring=featuring,
        title_raw=title_raw.strip(),
    )


def clean_title(raw_title: str, featuring: str | None = None) -> str:
    """Remove common non-title junk from YouTube titles.

    Strips things like [Official Music Video], (Lyrics), (4K), etc.
    Appends 'feat. X' if featuring is provided.
    """
    title = raw_title

    # Remove bracketed/parenthesized junk
    title = re.sub(
        r'\s*[\(\[]\s*(?:(?:official\s+)?(?:music\s+video|video|audio|visualizer|lyrics?|lyric\s+video)'
        r'(?:\s+[^\)\]]*)?|(?:4k|8k|hd|hq))\s*[\)\]]\s*',
        ' ',
        title,
        flags=re.IGNORECASE,
    )

    # Remove trailing separators like "- Lyrics" / "| Lyric Video"
    title = re.sub(
        r'\s*(?:[-–—|]\s*(?:lyrics?|lyric\s+video))\s*$',
        '',
        title,
        flags=re.IGNORECASE,
    )

    title = title.strip().strip('"""')

    if featuring:
        title = f"{title} feat. {featuring}"

    return title


def normalize_meta_text(s: str) -> str:
    """Normalize text for comparison — lowercase, replace & with 'and', strip non-alphanum."""
    if not s or not s.strip():
        return ""
    t = s.lower()
    t = t.replace("&", "and")
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t
