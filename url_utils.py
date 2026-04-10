"""YouTube URL normalization — port of Normalize-YouTubeUrl from the PS script."""

from urllib.parse import urlparse, parse_qs, urlencode
import re


def normalize_youtube_url(url: str) -> str:
    """Normalize YouTube URLs to standard watch?v= format.

    Handles:
      - youtu.be/<id>
      - youtube.com/shorts/<id>
      - Strips &list= playlist parameters (keeps only ?v=<id>)
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    host = (parsed.hostname or "").lower()

    # youtu.be/<id>
    if re.match(r"^(www\.)?youtu\.be$", host):
        video_id = parsed.path.strip("/")
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        return url

    # youtube.com
    if re.search(r"(^|\.)youtube\.com$", host):
        # /shorts/<id>
        m = re.match(r"/shorts/([^/?]+)", parsed.path)
        if m:
            video_id = m.group(1)
            return f"https://www.youtube.com/watch?v={video_id}"

        # /watch?v=<id>&list=... → /watch?v=<id>
        qs = parse_qs(parsed.query)
        v = qs.get("v", [None])[0]
        if v:
            return f"https://www.youtube.com/watch?v={v}"

    return url
