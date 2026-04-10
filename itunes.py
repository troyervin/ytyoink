"""iTunes API search and scoring — port of Get-ItunesMetadata from the PS script."""

import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

import requests

from metadata import normalize_meta_text


@dataclass
class ItunesMatch:
    song: str
    artist: str
    album: str
    year: str | None
    genre: str
    artwork_url: str | None
    score: int


def _score_results(results: list[dict], want_artist: str, want_title: str) -> ItunesMatch | None:
    """Score iTunes search results against wanted artist/title, return best match."""
    want_a = normalize_meta_text(want_artist)
    want_t = normalize_meta_text(want_title)

    best = None
    best_score = -1
    best_year = None
    best_is_single = None

    for r in results:
        a = normalize_meta_text(r.get("artistName", ""))
        t = normalize_meta_text(r.get("trackName", ""))
        if not a or not t:
            continue

        score = 0

        # Artist matching
        if a == want_a:
            score += 6
        elif want_a and (want_a in a or a in want_a):
            score += 3

        # Title matching
        if t == want_t:
            score += 6
        elif want_t and (want_t in t or t in want_t):
            score += 3

        # Bonus for having collection/release info
        if r.get("collectionName"):
            score += 1
        if r.get("releaseDate"):
            score += 1

        # Penalize singles
        cand_is_single = False
        collection = r.get("collectionName", "")
        if collection and re.search(r"(?i)\s*-\s*single$", collection):
            cand_is_single = True
            score -= 1

        # Parse year
        cand_year = None
        release_date = r.get("releaseDate")
        if release_date:
            try:
                cand_year = datetime.fromisoformat(release_date.replace("Z", "+00:00")).year
            except Exception:
                pass

        # Decide if this candidate beats the current best
        pick = False
        if score > best_score:
            pick = True
        elif score == best_score and best is not None:
            if best_is_single and not cand_is_single:
                pick = True
            elif best_is_single == cand_is_single and cand_year:
                if best_year is None or cand_year < best_year:
                    pick = True

        if pick:
            best_score = score
            best = r
            best_year = cand_year
            best_is_single = cand_is_single

    if not best:
        return None

    # Extract year
    year = None
    if best.get("releaseDate"):
        try:
            year = str(datetime.fromisoformat(best["releaseDate"].replace("Z", "+00:00")).year)
        except Exception:
            pass

    # Extract artwork URL (prefer 600px, fallback to 100px upscaled)
    artwork_url = None
    if best.get("artworkUrl600"):
        artwork_url = str(best["artworkUrl600"])
    elif best.get("artworkUrl100"):
        artwork_url = str(best["artworkUrl100"]).replace("100x100bb", "600x600bb")

    return ItunesMatch(
        song=str(best.get("trackName", "")),
        artist=str(best.get("artistName", "")),
        album=str(best.get("collectionName", "")),
        year=year,
        genre=str(best.get("primaryGenreName", "")),
        artwork_url=artwork_url,
        score=best_score,
    )


def search_itunes(artist: str, title: str) -> ItunesMatch | None:
    """Search iTunes for metadata matching the given artist and title.

    Tries both (artist, title) and (title, artist) orderings.
    Returns the best match, or None if nothing found.
    """
    if not (artist or "").strip() and not (title or "").strip():
        return None

    term = f"{artist} {title}".strip()
    if not term:
        return None

    url = (
        "https://itunes.apple.com/search?"
        + urllib.parse.urlencode({"term": term, "entity": "song", "limit": "10"})
    )

    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None

    # Score with normal ordering
    it1 = _score_results(results, artist, title)

    # Score with swapped ordering (handles reversed artist/title)
    it2 = None
    if artist and title:
        it2 = _score_results(results, title, artist)

    best = it1
    is_swap = False
    if it2 and (not it1 or it2.score > (it1.score + 2)):
        best = it2
        is_swap = True

    if best and is_swap:
        # When swapped ordering wins, the artist/song fields from iTunes
        # are already correct — no need to swap them ourselves
        pass

    return best
