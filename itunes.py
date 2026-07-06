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


def _result_to_match(r: dict, artist: str, title: str) -> ItunesMatch | None:
    """Score a single API result against the wanted artist/title."""
    m1 = _score_results([r], artist, title)
    m2 = _score_results([r], title, artist) if artist and title else None
    if m2 and (not m1 or m2.score > m1.score):
        return m2
    return m1


_APPLE_URL_RE = re.compile(
    r"music\.apple\.com/[a-z\-]+/(?:album|song)/[^/]+/(\d+)(?:\?i=(\d+))?",
    re.IGNORECASE)


def lookup_apple_music(url_or_id: str, artist: str = "",
                       title: str = "") -> list["ItunesMatch"]:
    """Resolve a music.apple.com link (or numeric id) via the lookup API.

    Reaches Apple Music streaming-only tracks that the iTunes Store
    search index doesn't contain at all.
    """
    m = _APPLE_URL_RE.search(url_or_id or "")
    if m:
        lookup_id = m.group(2) or m.group(1)
    elif (url_or_id or "").strip().isdigit():
        lookup_id = url_or_id.strip()
    else:
        return []
    try:
        resp = requests.get(
            "https://itunes.apple.com/lookup?"
            + urllib.parse.urlencode({"id": lookup_id, "entity": "song"}),
            timeout=8)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception:
        return []
    out = []
    for r in results:
        if r.get("wrapperType") != "track":
            continue
        match = _result_to_match(r, artist, title)
        if match:
            out.append(match)
    return out


def _apple_music_web_track_ids(term: str, limit: int = 5) -> list[str]:
    """Scrape Apple Music's web search for track ids — the fallback that
    finds streaming-only songs missing from the store search API."""
    url = ("https://music.apple.com/us/search?"
           + urllib.parse.urlencode({"term": term}))
    html = requests.get(url, timeout=10,
                        headers={"User-Agent": "Mozilla/5.0"}).text
    ids: list[str] = []
    for album_id, track_id in re.findall(r'/album/[^/"]+/(\d+)\?i=(\d+)', html):
        if track_id not in ids:
            ids.append(track_id)
        if len(ids) >= limit:
            break
    return ids


def search_itunes_album_tracks(album: str, artist: str = "",
                               limit: int = 30) -> list["ItunesMatch"]:
    """Find an album by name (optionally by artist) and return its tracks
    in album order — e.g. everything on 'The Blueprint' by Jay-Z."""
    term = f"{artist} {album}".strip()
    if not term:
        return []
    try:
        resp = requests.get(
            "https://itunes.apple.com/search?"
            + urllib.parse.urlencode({"term": term, "entity": "album",
                                      "limit": "10"}),
            timeout=8)
        resp.raise_for_status()
        albums = resp.json().get("results", [])
    except Exception:
        return []

    want_album = normalize_meta_text(album)
    want_artist = normalize_meta_text(artist)

    def album_score(a):
        name = normalize_meta_text(a.get("collectionName", ""))
        art = normalize_meta_text(a.get("artistName", ""))
        score = 0
        if want_album and want_album == name:
            score += 6
        elif want_album and want_album in name:
            score += 3
        if want_artist and (want_artist in art or art in want_artist):
            score += 4
        return score

    albums.sort(key=album_score, reverse=True)
    if not albums or album_score(albums[0]) <= 0:
        return []
    collection_id = albums[0].get("collectionId")
    if not collection_id:
        return []
    try:
        resp = requests.get(
            "https://itunes.apple.com/lookup?"
            + urllib.parse.urlencode({"id": collection_id, "entity": "song",
                                      "limit": str(limit)}),
            timeout=8)
        results = resp.json().get("results", [])
    except Exception:
        return []
    out = []
    for r in results:
        if r.get("wrapperType") != "track":
            continue
        match = _result_to_match(r, artist, "")
        if match:
            out.append(match)
    return out


def search_itunes_candidates(artist: str, title: str, limit: int = 6,
                             term: str | None = None) -> list["ItunesMatch"]:
    """Return the top-scoring iTunes matches (deduped), best first.

    Used by the "wrong match?" picker so the user can choose between
    e.g. the single, the album version, and remixes. `term` overrides the
    search text (user-edited query) while artist/title still drive scoring.
    """
    term = (term or f"{artist or ''} {title or ''}").strip()
    if not term:
        return []

    url = (
        "https://itunes.apple.com/search?"
        + urllib.parse.urlencode({"term": term, "entity": "song", "limit": "20"})
    )
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception:
        return []

    candidates = []
    seen = set()
    for r in results:
        m1 = _score_results([r], artist, title)
        m2 = _score_results([r], title, artist) if artist and title else None
        m = m1
        if m2 and (not m1 or m2.score > m1.score):
            m = m2
        if not m:
            continue
        key = (m.song.lower(), m.artist.lower(), m.album.lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append(m)

    candidates.sort(key=lambda m: m.score, reverse=True)

    # If nothing matched the wanted title BY THE WANTED ARTIST, the song may
    # be streaming-only (absent from the store search index) — check Apple
    # Music's web search and resolve the ids via the lookup API.
    want_t = normalize_meta_text(title or "")
    want_a = normalize_meta_text(artist or "")

    def _matches_want(c):
        if not want_t or want_t not in normalize_meta_text(c.song):
            return False
        if not want_a:
            return True
        cand_a = normalize_meta_text(c.artist)
        return want_a in cand_a or cand_a in want_a

    if not any(_matches_want(c) for c in candidates):
        try:
            ids = _apple_music_web_track_ids(term)
            if ids:
                resp = requests.get(
                    "https://itunes.apple.com/lookup?"
                    + urllib.parse.urlencode({"id": ",".join(ids),
                                              "entity": "song"}),
                    timeout=8)
                extra = []
                for r in resp.json().get("results", []):
                    if r.get("wrapperType") != "track":
                        continue
                    match = _result_to_match(r, artist, title)
                    if not match:
                        continue
                    key = (match.song.lower(), match.artist.lower(),
                           match.album.lower())
                    if key not in seen:
                        seen.add(key)
                        extra.append(match)
                candidates = extra + candidates
        except Exception:
            pass

    return candidates[:limit]


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
