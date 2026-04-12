import os
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Thread
from math import ceil
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo
import requests
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
import database as db

load_dotenv()

app = Flask(__name__, static_folder="static")
app.logger.setLevel(logging.INFO)

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
LIBRARY_PAGE_SIZE = 50
RECENT_TRACKS_PAGE_SIZE = 200
LOOKUP_PROGRESS_TTL_SECONDS = 15 * 60
LOOKUP_PROGRESS_DONE_TTL_SECONDS = 5 * 60
TRACK_PAGE_DATE_RE = re.compile(
    r'<span title="(?:[A-Z][a-z]+ )?([0-9]{1,2} [A-Z][a-z]{2} [0-9]{4}, [0-9]{1,2}:[0-9]{2}(?:am|pm))">'
)
TRACK_PAGE_PAGINATION_RE = re.compile(r'href="\?page=(\d+)"')
# Matches Last.fm library links of the form /music/Artist/_/TrackName.
# The anchored /music/ prefix and restricted character classes prevent ReDoS.
TRACK_LINK_IN_ARTIST_PAGE_RE = re.compile(r'href="/music/[^/]+/_/([^"?#/]+)"')


# Last.fm returns this hash for the default "star" placeholder — treat as no image
LASTFM_PLACEHOLDER_HASH = "2a96cbd8b46e442fc41c2b86b821562f"
LOOKUP_PROGRESS: dict[str, dict] = {}
LOOKUP_PROGRESS_LOCK = Lock()


def is_placeholder(url: str) -> bool:
    return LASTFM_PLACEHOLDER_HASH in url if url else True


def lastfm_get(method: str, **params):
    params.update({"method": method, "api_key": LASTFM_API_KEY, "format": "json"})
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(LASTFM_BASE, params=params, timeout=15)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = requests.HTTPError(response=resp)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_exc


def normalize_lastfm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def extract_artist_name(value) -> str:
    if isinstance(value, dict):
        return value.get("#text") or value.get("name", "")
    return str(value or "")


def lookup_context(username: str, artist: str, track: str) -> str:
    return f"user={username!r} artist={artist!r} track={track!r}"


def cleanup_lookup_progress(now: float | None = None) -> None:
    now = now or time.time()
    stale_lookup_ids = []
    for lookup_id, payload in LOOKUP_PROGRESS.items():
        updated_at = payload.get("updated_at", now)
        ttl = (
            LOOKUP_PROGRESS_DONE_TTL_SECONDS
            if payload.get("active") is False
            else LOOKUP_PROGRESS_TTL_SECONDS
        )
        if now - updated_at > ttl:
            stale_lookup_ids.append(lookup_id)

    for lookup_id in stale_lookup_ids:
        LOOKUP_PROGRESS.pop(lookup_id, None)


def progress_percent(pages_checked: int | None, pages_total: int | None) -> int | None:
    if not pages_checked or not pages_total:
        return None
    if pages_total <= 0:
        return None
    return max(0, min(100, round((pages_checked / pages_total) * 100)))


def update_lookup_progress(lookup_id: str | None, **fields) -> None:
    if not lookup_id:
        return

    now = time.time()
    with LOOKUP_PROGRESS_LOCK:
        cleanup_lookup_progress(now)
        payload = LOOKUP_PROGRESS.get(lookup_id, {}).copy()
        payload.update(fields)
        payload["lookup_id"] = lookup_id
        payload["updated_at"] = now
        payload.setdefault("created_at", now)
        payload.setdefault("active", True)

        pages_checked = payload.get("pages_checked")
        pages_total = payload.get("pages_total")
        payload["progress_percent"] = progress_percent(pages_checked, pages_total)
        LOOKUP_PROGRESS[lookup_id] = payload


def finish_lookup_progress(lookup_id: str | None, **fields) -> None:
    update_lookup_progress(lookup_id, active=False, **fields)


def get_lookup_progress_payload(lookup_id: str | None) -> dict | None:
    if not lookup_id:
        return None

    with LOOKUP_PROGRESS_LOCK:
        cleanup_lookup_progress()
        payload = LOOKUP_PROGRESS.get(lookup_id)
        return payload.copy() if payload else None


def should_log_page_progress(page: int, total_pages: int) -> bool:
    if total_pages <= 10:
        return True
    if page in {1, total_pages}:
        return True
    if page <= 3 or page > total_pages - 3:
        return True
    return page % 10 == 0


def scrobble_matches_track(scrobble: dict, track: str, artist: str) -> bool:
    return (
        normalize_lastfm_text(scrobble.get("name", "")) == normalize_lastfm_text(track)
        and normalize_lastfm_text(extract_artist_name(scrobble.get("artist")))
        == normalize_lastfm_text(artist)
    )


def matching_scrobbles_on_page(scrobbles, track: str, artist: str) -> list[dict]:
    if isinstance(scrobbles, dict):
        scrobbles = [scrobbles]

    matches = []
    for scrobble in scrobbles:
        if scrobble.get("@attr", {}).get("nowplaying"):
            continue
        if not scrobble_matches_track(scrobble, track, artist):
            continue

        date_info = scrobble.get("date") or {}
        timestamp = str(date_info.get("uts", "") or "")
        if not timestamp:
            continue

        date_text = date_info.get("#text", "")
        if not date_text:
            date_text = datetime.fromtimestamp(
                int(timestamp), tz=timezone.utc
            ).strftime("%d %b %Y, %H:%M UTC")

        matches.append(
            {
                "track": scrobble.get("name", "") or track,
                "artist": extract_artist_name(scrobble.get("artist")) or artist,
                "date": date_text,
                "timestamp": timestamp,
            }
        )

    matches.sort(key=lambda item: int(item["timestamp"]))
    return matches


def earliest_scrobble_on_page(scrobbles, track: str, artist: str) -> tuple[str, str] | None:
    matches = matching_scrobbles_on_page(scrobbles, track, artist)
    if not matches:
        return None

    return matches[0]["date"], matches[0]["timestamp"]


def recent_tracks_history_summary(
    username: str, track: str, artist: str, lookup_id: str | None = None
) -> dict | None:
    """Scan recent-track history and return the first play plus the total match count."""

    first_page = lastfm_get(
        "user.getRecentTracks",
        user=username,
        limit=RECENT_TRACKS_PAGE_SIZE,
        page=1,
    )
    recenttracks = first_page.get("recenttracks", {})
    total_pages = int(recenttracks.get("@attr", {}).get("totalPages", 1) or 1)
    app.logger.info(
        "recent-track summary scan started %s total_pages=%s page_size=%s",
        lookup_context(username, artist, track),
        total_pages,
        RECENT_TRACKS_PAGE_SIZE,
    )
    update_lookup_progress(
        lookup_id,
        stage="recent-track-summary",
        status="Scanning recent-track pages for play count",
        detail="Last.fm did not report a track playcount, so the app is deriving it from recent-track pages.",
        pages_checked=0,
        pages_total=total_pages,
    )

    first_match = None
    total_matches = 0

    for page in range(total_pages, 0, -1):
        pages_checked = total_pages - page + 1
        data = first_page
        if page != 1:
            data = lastfm_get(
                "user.getRecentTracks",
                user=username,
                limit=RECENT_TRACKS_PAGE_SIZE,
                page=page,
            )

        page_matches = matching_scrobbles_on_page(
            data.get("recenttracks", {}).get("track", []), track, artist
        )
        if page_matches or should_log_page_progress(page, total_pages):
            app.logger.info(
                "recent-track summary progress %s page=%s/%s matches_on_page=%s total_matches=%s",
                lookup_context(username, artist, track),
                page,
                total_pages,
                len(page_matches),
                total_matches + len(page_matches),
            )
        update_lookup_progress(
            lookup_id,
            stage="recent-track-summary",
            status="Scanning recent-track pages for play count",
            detail=(
                f"Checked {pages_checked} of {total_pages} recent-track pages while estimating track history."
            ),
            pages_checked=pages_checked,
            pages_total=total_pages,
        )
        if not page_matches:
            continue

        if first_match is None:
            first_match = page_matches[0]
        total_matches += len(page_matches)

    if not first_match:
        app.logger.info(
            "recent-track summary scan finished with no matches %s total_pages=%s",
            lookup_context(username, artist, track),
            total_pages,
        )
        finish_lookup_progress(
            lookup_id,
            stage="recent-track-summary-finished",
            status="Recent-track summary finished",
            detail="No matching scrobbles were found while estimating the track history.",
            pages_checked=total_pages,
            pages_total=total_pages,
        )
        return None

    app.logger.info(
        "recent-track summary scan found earliest match %s timestamp=%s total_matches=%s",
        lookup_context(username, artist, track),
        first_match["timestamp"],
        total_matches,
    )

    update_lookup_progress(
        lookup_id,
        stage="recent-track-summary-finished",
        status="Recent-track summary finished",
        detail=(
            f"Checked all {total_pages} recent-track pages and found {total_matches} matching scrobbles."
        ),
        pages_checked=total_pages,
        pages_total=total_pages,
    )
    return {
        "track": first_match["track"],
        "artist": first_match["artist"],
        "date": first_match["date"],
        "timestamp": first_match["timestamp"],
        "total_scrobbles": total_matches,
    }


def recent_tracks_first_listen(
    username: str, track: str, artist: str, lookup_id: str | None = None
) -> tuple[str, str] | tuple[None, None]:
    """Find the first play by scanning paginated recent tracks from oldest to newest."""

    first_page = lastfm_get(
        "user.getRecentTracks",
        user=username,
        limit=RECENT_TRACKS_PAGE_SIZE,
        page=1,
    )
    recenttracks = first_page.get("recenttracks", {})
    total_pages = int(recenttracks.get("@attr", {}).get("totalPages", 1) or 1)
    app.logger.info(
        "recent-track fallback scan started %s total_pages=%s page_size=%s",
        lookup_context(username, artist, track),
        total_pages,
        RECENT_TRACKS_PAGE_SIZE,
    )
    update_lookup_progress(
        lookup_id,
        stage="recent-track-fallback",
        status="Scanning older pages",
        detail="Fallback mode is active: the app is stepping backward through recent-track pages to find the earliest exact scrobble.",
        pages_checked=0,
        pages_total=total_pages,
    )

    for page in range(total_pages, 0, -1):
        pages_checked = total_pages - page + 1
        data = first_page
        if page != 1:
            data = lastfm_get(
                "user.getRecentTracks",
                user=username,
                limit=RECENT_TRACKS_PAGE_SIZE,
                page=page,
            )

        match = earliest_scrobble_on_page(
            data.get("recenttracks", {}).get("track", []), track, artist
        )
        if match or should_log_page_progress(page, total_pages):
            app.logger.info(
                "recent-track fallback progress %s page=%s/%s match_found=%s",
                lookup_context(username, artist, track),
                page,
                total_pages,
                bool(match),
            )
        update_lookup_progress(
            lookup_id,
            stage="recent-track-fallback",
            status="Still scanning older pages",
            detail=(
                f"Checked {pages_checked} of {total_pages} recent-track pages while walking backward through your history."
            ),
            pages_checked=pages_checked,
            pages_total=total_pages,
        )
        if match:
            app.logger.info(
                "recent-track fallback resolved first listen %s timestamp=%s",
                lookup_context(username, artist, track),
                match[1],
            )
            finish_lookup_progress(
                lookup_id,
                stage="recent-track-fallback-finished",
                status="Older page scan finished",
                detail=(
                    f"Found a matching scrobble after checking {pages_checked} of {total_pages} recent-track pages."
                ),
                pages_checked=pages_checked,
                pages_total=total_pages,
            )
            return match

    app.logger.info(
        "recent-track fallback finished with no exact match %s total_pages=%s",
        lookup_context(username, artist, track),
        total_pages,
    )
    finish_lookup_progress(
        lookup_id,
        stage="recent-track-fallback-finished",
        status="Older page scan finished",
        detail=f"Checked all {total_pages} recent-track pages without finding an exact match.",
        pages_checked=total_pages,
        pages_total=total_pages,
    )
    return None, None


def public_library_first_listen_date(
    username: str,
    artist: str,
    track: str,
    total_scrobbles: int,
    lookup_id: str | None = None,
) -> str | None:
    """Scrape the public track page from Last.fm and return the oldest scrobble date.

    The public library page exposes exact per-track scrobble timestamps even when the
    API cannot reliably locate sparse plays through weekly charts.
    """

    base_url = (
        f"https://www.last.fm/user/{quote(username, safe='')}/library/music/"
        f"{quote(artist, safe='')}/_/{quote(track, safe='')}"
    )
    headers = {"User-Agent": "lastfm-timetraveler/1.0"}
    context = lookup_context(username, artist, track)

    app.logger.info(
        "public track page lookup started %s total_scrobbles=%s",
        context,
        total_scrobbles,
    )
    update_lookup_progress(
        lookup_id,
        stage="public-track-page",
        status="Trying the public track page",
        detail="Checking whether Last.fm exposes the oldest visible scrobble on the public track page.",
        pages_checked=None,
        pages_total=None,
    )

    resp = requests.get(base_url, headers=headers, timeout=20)
    if resp.status_code == 404:
        app.logger.info("public track page returned 404 %s", context)
        update_lookup_progress(
            lookup_id,
            stage="public-track-page",
            status="Public track page unavailable",
            detail="The public track page returned 404, so the app will fall back to recent-track scanning if needed.",
        )
        return None
    resp.raise_for_status()
    if resp.history or "/login" in resp.url:
        app.logger.info(
            "public track page redirected before parsing %s final_url=%s; falling back to recent tracks",
            context,
            resp.url,
        )
        update_lookup_progress(
            lookup_id,
            stage="public-track-page",
            status="Public track page requires login",
            detail="Last.fm redirected the older public track page to login, so the app has to scan recent-track pages instead.",
        )
        return None

    page_count = max(
        [int(m) for m in TRACK_PAGE_PAGINATION_RE.findall(resp.text)] or [1]
    )
    if total_scrobbles > 0:
        page_count = max(page_count, ceil(total_scrobbles / LIBRARY_PAGE_SIZE))
    app.logger.info(
        "public track page parsed %s inferred_page_count=%s",
        context,
        page_count,
    )
    update_lookup_progress(
        lookup_id,
        stage="public-track-page",
        status="Trying the public track page",
        detail=(
            f"The public track page suggests about {page_count} pages of scrobbles for this track."
        ),
    )

    last_page_html = resp.text
    if page_count > 1:
        last_page_url = f"{base_url}?page={page_count}"
        app.logger.info(
            "public track page fetching oldest visible page %s page=%s",
            context,
            page_count,
        )
        update_lookup_progress(
            lookup_id,
            stage="public-track-page",
            status="Trying the public track page",
            detail=f"Fetching public track page {page_count} to look for the oldest visible scrobble.",
        )
        last_resp = requests.get(last_page_url, headers=headers, timeout=20)
        last_resp.raise_for_status()
        if last_resp.history or "/login" in last_resp.url or last_resp.url != last_page_url:
            app.logger.info(
                "public track page redirected while fetching oldest visible page %s requested_url=%s final_url=%s; falling back to recent tracks",
                context,
                last_page_url,
                last_resp.url,
            )
            update_lookup_progress(
                lookup_id,
                stage="public-track-page",
                status="Public track page requires login",
                detail="Last.fm redirected the older public track page to login, so the app has to scan recent-track pages instead.",
            )
            return None
        last_page_html = last_resp.text

    matches = TRACK_PAGE_DATE_RE.findall(last_page_html)
    if not matches:
        app.logger.info("public track page exposed no dated scrobbles %s", context)
        update_lookup_progress(
            lookup_id,
            stage="public-track-page",
            status="Public track page has no dated scrobbles",
            detail="The public track page loaded, but it did not expose the oldest exact timestamp needed for this lookup.",
        )
        return None

    app.logger.info(
        "public track page resolved earliest visible scrobble %s date=%s",
        context,
        matches[-1],
    )
    update_lookup_progress(
        lookup_id,
        stage="public-track-page-finished",
        status="Public track page resolved",
        detail=f"Found an earliest visible scrobble on the public track page: {matches[-1]}.",
    )

    return matches[-1]


def lastfm_library_date_to_timestamp(date_text: str) -> str:
    """Convert a Last.fm library page date to a unix timestamp string."""
    for fmt in ("%d %b %Y, %I:%M%p", "%d %b %Y, %H:%M"):
        try:
            dt = datetime.strptime(date_text, fmt)
            dt = dt.replace(tzinfo=ZoneInfo("Europe/Vienna"))
            return str(int(dt.timestamp()))
        except ValueError:
            continue
    raise ValueError(f"Unsupported Last.fm date format: {date_text}")


def _oldest_scrobble_on_track_page(
    username: str,
    artist: str,
    track_name_encoded: str,
    headers: dict,
) -> tuple[str, str] | tuple[None, None]:
    """Fetch the per-track scrobble page and return (date, timestamp) of the oldest scrobble."""
    track_url = (
        f"https://www.last.fm/user/{quote(username, safe='')}/library/music/"
        f"{quote(artist, safe='')}/_/{track_name_encoded}"
    )
    resp = requests.get(track_url, headers=headers, timeout=20)
    if resp.status_code != 200 or "/login" in resp.url:
        return None, None

    track_page_count = max(
        [int(m) for m in TRACK_PAGE_PAGINATION_RE.findall(resp.text)] or [1]
    )
    last_page_html = resp.text
    if track_page_count > 1:
        last_resp = requests.get(
            f"{track_url}?page={track_page_count}", headers=headers, timeout=20
        )
        if last_resp.status_code == 200 and "/login" not in last_resp.url:
            last_page_html = last_resp.text

    dates = TRACK_PAGE_DATE_RE.findall(last_page_html)
    if not dates:
        return None, None

    oldest_date = dates[-1]
    try:
        oldest_ts = lastfm_library_date_to_timestamp(oldest_date)
    except ValueError:
        return None, None
    return oldest_date, oldest_ts


def public_library_artist_first_listen(
    username: str,
    artist: str,
    total_artist_scrobbles: int,
) -> tuple[str, str, str] | tuple[None, None, None]:
    """Find the oldest scrobble of *artist* by checking per-track scrobble pages.

    The artist library page (``/user/{u}/library/music/{a}``) lists the user's
    tracks for an artist ordered by play count but does **not** expose individual
    scrobble timestamps.  Per-track pages (``…/_/{track}``) *do* show timestamps,
    so this function collects the track list and then checks per-track pages —
    prioritising the least-played tracks which are more likely to include the
    very first listen.

    Returns ``(date, timestamp, track_name)`` or ``(None, None, None)``.
    """
    base_url = (
        f"https://www.last.fm/user/{quote(username, safe='')}/library/music/"
        f"{quote(artist, safe='')}"
    )
    headers = {"User-Agent": "lastfm-timetraveler/1.0"}
    context = lookup_context(username, artist, "")

    app.logger.info(
        "artist first-listen lookup started %s total_artist_scrobbles=%s",
        context,
        total_artist_scrobbles,
    )

    resp = requests.get(base_url, headers=headers, timeout=20)
    if resp.status_code == 404:
        app.logger.info("artist library page returned 404 %s", context)
        return None, None, None
    resp.raise_for_status()
    if "/login" in resp.url:
        app.logger.info(
            "artist library page redirected to login %s final_url=%s", context, resp.url
        )
        return None, None, None

    # Collect track names from all pages of the artist library (ordered most→least played).
    all_track_names: list[str] = list(
        dict.fromkeys(TRACK_LINK_IN_ARTIST_PAGE_RE.findall(resp.text))
    )

    page_count = max(
        [int(m) for m in TRACK_PAGE_PAGINATION_RE.findall(resp.text)] or [1]
    )
    # Fetch remaining pages to gather more track names.
    for page_num in range(2, page_count + 1):
        page_resp = requests.get(
            f"{base_url}?page={page_num}", headers=headers, timeout=20
        )
        if page_resp.status_code != 200 or "/login" in page_resp.url:
            break
        for t in TRACK_LINK_IN_ARTIST_PAGE_RE.findall(page_resp.text):
            if t not in dict.fromkeys(all_track_names):
                all_track_names.append(t)

    if not all_track_names:
        app.logger.info("artist library page listed no tracks %s", context)
        return None, None, None

    app.logger.info(
        "artist first-listen checking per-track pages %s tracks_found=%s",
        context,
        len(all_track_names),
    )

    # Check per-track scrobble pages.  Prioritise the least-played tracks
    # (end of the list) because they are more likely to include the oldest listen,
    # but also include a few of the most-played tracks.  Cap total requests.
    MAX_TRACK_CHECKS = 10
    candidates: list[str] = []
    # Least-played first (reversed tail)
    candidates.extend(reversed(all_track_names))
    # Add most-played that aren't already included
    for t in all_track_names:
        if t not in candidates:
            candidates.append(t)
    candidates = candidates[:MAX_TRACK_CHECKS]

    best_date: str | None = None
    best_ts: str | None = None
    best_track: str = ""

    for track_encoded in candidates:
        date, ts = _oldest_scrobble_on_track_page(
            username, artist, track_encoded, headers
        )
        if date and ts:
            if best_ts is None or int(ts) < int(best_ts):
                best_date = date
                best_ts = ts
                best_track = unquote(track_encoded.replace("+", " "))

    if best_date:
        app.logger.info(
            "artist first-listen resolved %s date=%s track=%s",
            context,
            best_date,
            best_track,
        )
        return best_date, best_ts, best_track

    app.logger.info("artist first-listen found no dated scrobbles %s", context)
    return None, None, None


def _find_and_store_artist_first_listen(username: str, artist: str) -> dict:
    """Look up (and cache) the first time *username* listened to any track by *artist*.

    Checks the database first; if not found, queries Last.fm for the artist play
    count then scrapes the artist library page.  The result is always persisted.

    Returns a dict with keys ``first_listen_date``, ``first_listen_timestamp``,
    ``first_listen_track``.  Values may be empty strings if unavailable.
    """
    cached = db.get_artist_first_listen(username, artist)
    if cached and cached.get("first_listen_date"):
        return {
            "first_listen_date": cached["first_listen_date"],
            "first_listen_timestamp": cached["first_listen_timestamp"],
            "first_listen_track": cached.get("first_listen_track", ""),
        }

    total_artist_scrobbles = 0
    try:
        artist_info = lastfm_get("artist.getInfo", artist=artist, username=username)
        stats = artist_info.get("artist", {}).get("stats", {})
        total_artist_scrobbles = int(stats.get("userplaycount", 0) or 0)
    except Exception:
        pass

    date, timestamp, track_name = None, None, ""
    try:
        result = public_library_artist_first_listen(
            username, artist, total_artist_scrobbles
        )
        if result[0] is not None:
            date, timestamp, track_name = result
    except Exception:
        app.logger.exception(
            "artist library page lookup failed user=%r artist=%r", username, artist
        )

    db.save_artist_first_listen(
        username=username,
        artist=artist,
        first_listen_track=track_name or "",
        first_listen_date=date or "",
        first_listen_timestamp=timestamp or "",
    )
    return {
        "first_listen_date": date or "",
        "first_listen_timestamp": timestamp or "",
        "first_listen_track": track_name or "",
    }


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def status():
    """Health check — verifies API key is configured."""
    if not LASTFM_API_KEY or LASTFM_API_KEY == "your_api_key_here":
        return jsonify({"ok": False, "error": "LASTFM_API_KEY is not set. Copy .env.example to .env and add your key."}), 200
    return jsonify({"ok": True})


@app.route("/api/ready")
def ready():
    """Readiness check — verifies the API key and database are usable."""
    if not LASTFM_API_KEY or LASTFM_API_KEY == "your_api_key_here":
        return jsonify({"ok": False, "error": "LASTFM_API_KEY is not set. Copy .env.example to .env and add your key."}), 503

    try:
        db.init_db()
    except Exception as exc:
        app.logger.exception("database readiness check failed")
        return jsonify({"ok": False, "error": f"Database is not ready: {exc}"}), 503

    return jsonify({"ok": True})


@app.route("/api/user/validate")
def validate_user():
    """Validate a Last.fm username and return profile info."""
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Username is required."}), 400
    try:
        data = lastfm_get("user.getInfo", user=username)
        user = data.get("user", {})
        image_url = ""
        for img in user.get("image", []):
            if img.get("size") == "medium" and img.get("#text") and not is_placeholder(img["#text"]):
                image_url = img["#text"]
        reg = user.get("registered", {})
        if isinstance(reg, dict):
            reg_text = reg.get("#text", "")
            reg_ts = reg.get("unixtime", "")
        else:
            reg_text = ""
            reg_ts = str(reg)
        if not reg_text and reg_ts:
            try:
                reg_date = datetime.fromtimestamp(int(reg_ts), tz=timezone.utc)
                reg_text = reg_date.strftime("%B %Y")
            except (ValueError, OSError):
                reg_text = ""
        return jsonify({
            "ok": True,
            "username": user.get("name", username),
            "playcount": int(user.get("playcount", 0)),
            "registered": reg_text,
            "image": image_url,
        })
    except requests.HTTPError:
        return jsonify({"ok": False, "error": f"User '{username}' not found on Last.fm."}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Last.fm API error: {exc}"}), 200


@app.route("/api/user/top-tracks")
def user_top_tracks():
    """Get a user's top tracks for suggestions."""
    username = request.args.get("username", "").strip()
    period = request.args.get("period", "1month")  # overall, 7day, 1month, 3month, 6month, 12month
    if not username:
        return jsonify({"error": "username is required"}), 400
    try:
        data = lastfm_get("user.getTopTracks", user=username, period=period, limit=10)
        tracks = data.get("toptracks", {}).get("track", [])
        results = []
        for t in tracks:
            track_name = t.get("name", "")
            artist_name = t.get("artist", {}).get("name", "")
            # user.getTopTracks only returns placeholder images;
            # fetch real album art from track.getInfo
            image_url = ""
            try:
                ti = lastfm_get("track.getInfo", track=track_name, artist=artist_name)
                for img in ti.get("track", {}).get("album", {}).get("image", []):
                    if img.get("size") == "medium" and img.get("#text") and not is_placeholder(img["#text"]):
                        image_url = img["#text"]
            except Exception:
                pass
            results.append({
                "name": track_name,
                "artist": artist_name,
                "image": image_url,
                "playcount": int(t.get("playcount", 0)),
            })
        return jsonify(results)
    except Exception:
        return jsonify([])


@app.route("/api/user/recent-tracks")
def user_recent_tracks():
    """Get a user's most recently scrobbled tracks."""
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "username is required"}), 400
    try:
        data = lastfm_get("user.getRecentTracks", user=username, limit=10)
        tracks = data.get("recenttracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        results = []
        for t in tracks:
            # Skip "now playing" entries which have no timestamp
            if t.get("@attr", {}).get("nowplaying"):
                continue
            artist = t.get("artist", {})
            artist_name = artist.get("#text", "") if isinstance(artist, dict) else str(artist)
            image_url = ""
            for img in t.get("image", []):
                if img.get("size") == "medium" and img.get("#text") and not is_placeholder(img["#text"]):
                    image_url = img["#text"]
            date_info = t.get("date", {})
            played_at = date_info.get("#text", "") if isinstance(date_info, dict) else ""
            results.append({
                "name": t.get("name", ""),
                "artist": artist_name,
                "image": image_url,
                "played_at": played_at,
            })
        return jsonify(results)
    except Exception:
        return jsonify([])


@app.route("/api/on-this-day")
def on_this_day():
    """Find what the user was listening to on this day 1, 5, and 10 years ago."""
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "username is required"}), 400

    now = datetime.now(timezone.utc)
    periods = []
    for years_ago in [1, 2, 3, 5, 10]:
        try:
            target = now.replace(year=now.year - years_ago)
        except ValueError:
            # Feb 29 edge case
            target = now.replace(year=now.year - years_ago, day=28)
        day_start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        from_ts = int(day_start.timestamp())
        to_ts = int(day_end.timestamp())

        try:
            data = lastfm_get(
                "user.getRecentTracks",
                user=username,
                limit=50,
                **{"from": str(from_ts), "to": str(to_ts)},
            )
            scrobbles = data.get("recenttracks", {}).get("track", [])
            if isinstance(scrobbles, dict):
                scrobbles = [scrobbles]
            # Filter out "now playing" entries
            scrobbles = [s for s in scrobbles if not s.get("@attr", {}).get("nowplaying")]

            # Count plays per track, keep order of first appearance
            seen = {}
            track_list = []
            for s in scrobbles:
                s_artist = s.get("artist", {})
                artist_name = s_artist.get("#text", "") if isinstance(s_artist, dict) else str(s_artist)
                key = (s.get("name", "").lower(), artist_name.lower())
                if key not in seen:
                    image_url = ""
                    for img in s.get("image", []):
                        if img.get("size") == "medium" and img.get("#text") and not is_placeholder(img["#text"]):
                            image_url = img["#text"]
                    seen[key] = len(track_list)
                    track_list.append({
                        "name": s.get("name", ""),
                        "artist": artist_name,
                        "image": image_url,
                        "plays": 1,
                    })
                else:
                    track_list[seen[key]]["plays"] += 1

            # Sort by plays descending, take top 6
            track_list.sort(key=lambda x: x["plays"], reverse=True)
            top = track_list[:6]

            if top:
                periods.append({
                    "years_ago": years_ago,
                    "date": day_start.strftime("%B %d, %Y"),
                    "tracks": top,
                    "total_scrobbles": len(scrobbles),
                })
        except Exception:
            continue

    return jsonify(periods)


@app.route("/api/search")
def search_tracks():
    """Autocomplete: search Last.fm tracks by name."""
    query = request.args.get("q", "").strip()
    if not query or len(query) < 2:
        return jsonify([])

    data = lastfm_get("track.search", track=query, limit=8)
    matches = data.get("results", {}).get("trackmatches", {}).get("track", [])

    results = []
    for t in matches:
        image_url = ""
        images = t.get("image", [])
        for img in images:
            if img.get("size") == "medium" and img.get("#text") and not is_placeholder(img["#text"]):
                image_url = img["#text"]
        results.append(
            {
                "name": t.get("name", ""),
                "artist": t.get("artist", ""),
                "image": image_url,
                "listeners": int(t.get("listeners", 0)),
            }
        )
    results.sort(key=lambda r: r["listeners"], reverse=True)
    return jsonify(results)


@app.route("/api/lookup-progress")
def lookup_progress():
    lookup_id = request.args.get("lookup_id", "").strip()
    if not lookup_id:
        return jsonify({"error": "lookup_id is required"}), 400

    payload = get_lookup_progress_payload(lookup_id)
    if not payload:
        return jsonify({"found": False}), 200

    return jsonify({"found": True, **payload})


def _run_first_listen_lookup(
    username: str, track: str, artist: str, lookup_id: str, flask_app
) -> None:
    """Background worker for the first-listen lookup.

    Runs all the slow Last.fm page scanning and stores the final result in
    LOOKUP_PROGRESS so the client can retrieve it via ``/api/lookup-progress``.
    """
    started_at = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started_at) * 1000)

    with flask_app.app_context():
        try:
            _do_first_listen_lookup(username, track, artist, lookup_id, elapsed_ms)
        except Exception:
            flask_app.logger.exception(
                "background lookup failed %s", lookup_context(username, artist, track)
            )
            finish_lookup_progress(
                lookup_id,
                stage="lookup-finished",
                status="Lookup failed",
                detail="An unexpected error occurred during the lookup.",
                result={
                    "error": "Internal lookup error",
                    "elapsed_ms": elapsed_ms(),
                },
            )


def _do_first_listen_lookup(
    username: str, track: str, artist: str, lookup_id: str, elapsed_ms
) -> None:
    # Step 1: Check total play count via track.getInfo (fast, single call)
    try:
        info = lastfm_get(
            "track.getInfo", track=track, artist=artist, username=username
        )
    except Exception:
        finish_lookup_progress(
            lookup_id,
            username=username,
            artist=artist,
            track=track,
            stage="request-error",
            status="Lookup failed",
            detail="Last.fm returned an API error while loading track metadata.",
            result={"error": "Last.fm API error", "elapsed_ms": elapsed_ms()},
        )
        return

    track_info = info.get("track") or {}
    history_summary = None
    userplaycount = track_info.get("userplaycount")
    total = int(userplaycount or 0)
    app.logger.info(
        "track metadata loaded %s userplaycount=%s",
        lookup_context(username, artist, track),
        userplaycount,
    )
    update_lookup_progress(
        lookup_id,
        username=username,
        artist=artist,
        track=track,
        stage="track-metadata-loaded",
        status="Checking Last.fm track metadata",
        detail=(
            f"Last.fm reports {total} scrobbles for this track." if total else "Track metadata loaded."
        ),
    )

    if userplaycount in (None, ""):
        try:
            history_summary = recent_tracks_history_summary(username, track, artist, lookup_id)
        except requests.RequestException:
            history_summary = None

        if history_summary:
            total = history_summary["total_scrobbles"]
            app.logger.info(
                "derived playcount from recent-track summary %s total_scrobbles=%s",
                lookup_context(username, artist, track),
                total,
            )

    if total == 0:
        app.logger.info("lookup found no scrobbles %s", lookup_context(username, artist, track))
        finish_lookup_progress(
            lookup_id,
            username=username,
            artist=artist,
            track=track,
            stage="lookup-finished",
            status="No matching scrobbles",
            detail="Last.fm does not report any scrobbles for this track under this user.",
            pages_checked=1,
            pages_total=1,
            result={
                "found": False,
                "track": track,
                "artist": artist,
                "message": "You have never listened to this track.",
                "cached": False,
                "elapsed_ms": elapsed_ms(),
            },
        )
        return

    # Gather album art / album name from the same track.getInfo response
    image_url = ""
    album_name = ""
    album_data = track_info.get("album") or {}
    album_name = album_data.get("title", "")
    for img in album_data.get("image", []):
        if img.get("size") == "extralarge" and img.get("#text") and not is_placeholder(img["#text"]):
            image_url = img["#text"]

    # Canonical names from Last.fm
    canonical_track = track_info.get("name", track)
    canonical_artist = (track_info.get("artist") or {}).get("name", artist)
    exact_date = history_summary["date"] if history_summary else None
    exact_ts = history_summary["timestamp"] if history_summary else ""
    date_unavailable_reason = ""

    if history_summary:
        canonical_track = history_summary["track"] or canonical_track
        canonical_artist = history_summary["artist"] or canonical_artist

    if not exact_date:
        # Save the confirmed lookup metadata before the slower date-resolution
        # fallbacks so the search still appears in history if the request takes
        # a long time or is interrupted.
        app.logger.info(
            "saving partial lookup before slow date resolution %s total_scrobbles=%s",
            lookup_context(username, canonical_artist, canonical_track),
            total,
        )
        db.save_result(
            username,
            canonical_track,
            canonical_artist,
            album_name,
            "",
            "",
            total,
            image_url,
        )

    if not exact_date:
        try:
            exact_date = public_library_first_listen_date(
                username, canonical_artist, canonical_track, total, lookup_id
            )
            if exact_date:
                exact_ts = lastfm_library_date_to_timestamp(exact_date)
        except requests.RequestException:
            date_unavailable_reason = (
                "The public Last.fm track page could not be fetched, so the exact first-listen timestamp could not be determined."
            )
            update_lookup_progress(
                lookup_id,
                stage="public-track-page-error",
                status="Public track page failed",
                detail="The public track page request failed, so the app is switching to recent-track scanning.",
            )
        except ValueError:
            date_unavailable_reason = (
                "The public Last.fm track page exposed a date, but it could not be converted into a timestamp."
            )
            update_lookup_progress(
                lookup_id,
                stage="public-track-page-error",
                status="Public track page parsing failed",
                detail="The public page exposed a date, but the timestamp could not be parsed cleanly.",
            )

    if not exact_date:
        try:
            app.logger.info(
                "falling back to recent-track scan %s",
                lookup_context(username, canonical_artist, canonical_track),
            )
            exact_date, exact_ts = recent_tracks_first_listen(
                username, canonical_track, canonical_artist, lookup_id
            )
        except requests.RequestException:
            date_unavailable_reason = (
                "The accessible Last.fm listening history could not be fetched, so the exact first-listen timestamp could not be determined."
            )
            update_lookup_progress(
                lookup_id,
                stage="recent-track-fallback-error",
                status="Older page scan failed",
                detail="The recent-track scan failed while walking backward through the listening history.",
            )

    if not exact_date:
        date_unavailable_reason = date_unavailable_reason or (
            "Last.fm reports plays for this track, but neither the public track page nor the accessible recent-track history exposed an exact first-listen timestamp."
        )

    date_unavailable = not bool(exact_date)

    # Persist the result so future queries are served from the local cache
    db.save_result(
        username,
        canonical_track,
        canonical_artist,
        album_name,
        exact_date or "",
        exact_ts or "",
        total,
        image_url,
    )

    # Look up (and cache) when this artist was first heard by the user
    artist_first = _find_and_store_artist_first_listen(username, canonical_artist)

    app.logger.info(
        "lookup finished %s date_found=%s cached=%s elapsed_ms=%s",
        lookup_context(username, canonical_artist, canonical_track),
        bool(exact_date),
        False,
        elapsed_ms(),
    )
    current_progress = get_lookup_progress_payload(lookup_id) or {}
    finish_lookup_progress(
        lookup_id,
        username=username,
        artist=canonical_artist,
        track=canonical_track,
        stage="lookup-finished",
        status="Lookup finished",
        detail=(
            "Found the earliest exact scrobble timestamp."
            if exact_date
            else "The lookup finished, but Last.fm did not expose an exact first-listen timestamp."
        ),
        pages_checked=current_progress.get("pages_checked"),
        pages_total=current_progress.get("pages_total"),
        result={
            "found": True,
            "track": canonical_track,
            "artist": canonical_artist,
            "album": album_name,
            "date": exact_date or "",
            "timestamp": exact_ts or "",
            "total_scrobbles": total,
            "image": image_url,
            "date_unavailable": date_unavailable,
            "date_unavailable_reason": date_unavailable_reason,
            "cached": False,
            "elapsed_ms": elapsed_ms(),
            "artist_first_listen_date": artist_first["first_listen_date"],
            "artist_first_listen_timestamp": artist_first["first_listen_timestamp"],
            "artist_first_listen_track": artist_first["first_listen_track"],
        },
    )


@app.route("/api/first-listen")
def first_listen():
    """Find the very first scrobble of a track for the given user.

    The lookup is executed in a background thread; the endpoint returns
    immediately with HTTP 202.  The client polls ``/api/lookup-progress``
    to get progress updates and the final result.
    """
    started_at = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started_at) * 1000)

    lookup_id = request.args.get("lookup_id", "").strip() or None
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    username = request.args.get("username", "").strip()

    if not track or not artist or not username:
        finish_lookup_progress(
            lookup_id,
            stage="request-invalid",
            status="Lookup failed",
            detail="The lookup request is missing the required track, artist, or username.",
        )
        return jsonify({
            "error": "track, artist, and username are required",
            "elapsed_ms": elapsed_ms(),
        }), 400

    if not lookup_id:
        lookup_id = f"server-{int(time.time() * 1000)}"

    # Return cached result immediately if available (first-listen date never changes)
    # Skip cache entries that have no date — those are transient failures worth retrying.
    cached = db.get_cached(username, track, artist)
    if cached and cached.get("first_listen_date"):
        app.logger.info(
            "lookup served from cache %s elapsed_ms=%s",
            lookup_context(username, artist, track),
            elapsed_ms(),
        )
        cached_timestamp = cached["first_listen_timestamp"] or ""
        cached_date = cached["first_listen_date"] or ""
        date_unavailable = not bool(cached_date)
        db.save_result(
            username,
            cached["track"],
            cached["artist"],
            cached["album"] or "",
            cached_date,
            cached_timestamp,
            cached["total_scrobbles"] or 0,
            cached["image"] or "",
        )
        cached_artist = cached["artist"]

        # Check if artist first-listen is already cached
        artist_first_cached = db.get_artist_first_listen(username, cached_artist)
        artist_first_result = {}
        if artist_first_cached and artist_first_cached.get("first_listen_date"):
            # Include cached artist data immediately if available
            artist_first_result = {
                "artist_first_listen_date": artist_first_cached.get("first_listen_date", ""),
                "artist_first_listen_timestamp": artist_first_cached.get("first_listen_timestamp", ""),
                "artist_first_listen_track": artist_first_cached.get("first_listen_track", ""),
            }
        # Otherwise, omit the fields and let the UI fetch them asynchronously

        finish_lookup_progress(
            lookup_id,
            username=username,
            artist=cached_artist,
            track=cached["track"],
            stage="cache-hit",
            status="Loaded from cache",
            detail="This lookup was already cached locally, so no page scan was needed.",
            pages_checked=1,
            pages_total=1,
            result={
                "found": True,
                "track": cached["track"],
                "artist": cached_artist,
                "album": cached["album"] or "",
                "date": cached_date,
                "timestamp": cached_timestamp,
                "total_scrobbles": cached["total_scrobbles"],
                "image": cached["image"] or "",
                "date_unavailable": date_unavailable,
                "date_unavailable_reason": (
                    "Last.fm reports a play for this track, but the public data exposed to the app does not include an exact first-listen timestamp."
                    if date_unavailable
                    else ""
                ),
                "cached": True,
                "elapsed_ms": elapsed_ms(),
                **artist_first_result,
            },
        )
        return jsonify(
            {
                "found": True,
                "track": cached["track"],
                "artist": cached_artist,
                "album": cached["album"] or "",
                "date": cached_date,
                "timestamp": cached_timestamp,
                "total_scrobbles": cached["total_scrobbles"],
                "image": cached["image"] or "",
                "date_unavailable": date_unavailable,
                "date_unavailable_reason": (
                    "Last.fm reports a play for this track, but the public data exposed to the app does not include an exact first-listen timestamp."
                    if date_unavailable
                    else ""
                ),
                "cached": True,
                "elapsed_ms": elapsed_ms(),
                **artist_first_result,
            }
        )

    # Start the lookup in a background thread
    app.logger.info("lookup request accepted (async) %s", lookup_context(username, artist, track))
    update_lookup_progress(
        lookup_id,
        username=username,
        artist=artist,
        track=track,
        stage="request-started",
        status="Checking Last.fm track metadata",
        detail="Starting lookup and checking basic track metadata.",
        pages_checked=None,
        pages_total=None,
    )
    thread = Thread(
        target=_run_first_listen_lookup,
        args=(username, track, artist, lookup_id, app),
        daemon=True,
    )
    thread.start()

    return jsonify({"accepted": True, "lookup_id": lookup_id}), 202


@app.route("/api/artist-image")
def artist_image():
    """Return the image URL for a given artist."""
    artist = request.args.get("artist", "").strip()
    if not artist:
        return jsonify({"image": ""})
    try:
        data = lastfm_get("artist.getInfo", artist=artist)
        images = data.get("artist", {}).get("image", [])
        image_url = ""
        for img in images:
            if img.get("size") == "extralarge" and img.get("#text") and not is_placeholder(img["#text"]):
                image_url = img["#text"]
        if not image_url:
            for img in images:
                if img.get("size") == "medium" and img.get("#text") and not is_placeholder(img["#text"]):
                    image_url = img["#text"]
        # Fallback: use the top album's cover art
        if not image_url:
            try:
                albums = lastfm_get("artist.getTopAlbums", artist=artist, limit=1)
                for album in albums.get("topalbums", {}).get("album", []):
                    for img in album.get("image", []):
                        if img.get("size") == "extralarge" and img.get("#text") and not is_placeholder(img["#text"]):
                            image_url = img["#text"]
                            break
                    if image_url:
                        break
            except Exception:
                pass
        return jsonify({"image": image_url})
    except Exception:
        return jsonify({"image": ""})


@app.route("/api/history")
def history():
    """Return all previously resolved first-listen results for the configured user."""
    username = request.args.get("username", "").strip()
    results = db.get_history(username)
    return jsonify(
        [
            {
                "track": r["track"],
                "artist": r["artist"],
                "album": r["album"] or "",
                "date": r["first_listen_date"],
                "timestamp": r["first_listen_timestamp"],
                "total_scrobbles": r["total_scrobbles"],
                "image": r["image"] or "",
                "queried_at": r["queried_at"],
            }
            for r in results
        ]
    )


@app.route("/api/artist-first-listen")
def artist_first_listen():
    """Return the earliest known scrobble of any track by *artist* for the given user.

    If the result is already cached in the database it is returned immediately.
    Otherwise a live lookup against the Last.fm public library page is performed
    and the result is stored for future calls.
    """
    username = request.args.get("username", "").strip()
    artist = request.args.get("artist", "").strip()

    if not username or not artist:
        return jsonify({"error": "username and artist are required"}), 400

    result = _find_and_store_artist_first_listen(username, artist)
    return jsonify(
        {
            "artist": artist,
            "username": username,
            "first_listen_date": result["first_listen_date"],
            "first_listen_timestamp": result["first_listen_timestamp"],
            "first_listen_track": result["first_listen_track"],
        }
    )


LISTENING_HISTORY_CACHE: dict[str, dict] = {}
LISTENING_HISTORY_CACHE_LOCK = Lock()
LISTENING_HISTORY_CACHE_TTL_SECONDS = 30 * 60
LISTENING_HISTORY_MAX_WORKERS = 6


def _listening_history_cache_key(username: str, track: str, artist: str, months: int) -> str:
    return "|".join([
        normalize_lastfm_text(username),
        normalize_lastfm_text(track),
        normalize_lastfm_text(artist),
        str(months),
    ])


def _fetch_week_plays(
    username: str, week: dict, norm_track: str, norm_artist: str
) -> int:
    """Fetch a single weekly track chart and return the play count for the target track."""
    try:
        weekly_data = lastfm_get(
            "user.getWeeklyTrackChart",
            user=username,
            **{"from": week["from"], "to": week["to"]},
        )
    except Exception:
        return 0

    week_tracks = weekly_data.get("weeklytrackchart", {}).get("track", [])
    if isinstance(week_tracks, dict):
        week_tracks = [week_tracks]
    for t in week_tracks:
        t_name = normalize_lastfm_text(t.get("name", ""))
        t_artist = normalize_lastfm_text(extract_artist_name(t.get("artist")))
        if t_name == norm_track and t_artist == norm_artist:
            return int(t.get("playcount", 0))
    return 0


@app.route("/api/listening-history")
def listening_history():
    """Return monthly play counts for a track over the user's scrobble history.

    Uses the Last.fm weekly chart list to identify chart periods, then queries
    weekly track charts **in parallel** to collect play counts, aggregated by
    calendar month.  Results are cached for 30 minutes.
    """
    username = request.args.get("username", "").strip()
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    months_param = request.args.get("months", "12").strip()

    if not username or not track or not artist:
        return jsonify({"error": "username, track, and artist are required"}), 400

    try:
        max_months = min(int(months_param), 36)
    except (ValueError, TypeError):
        max_months = 12

    # Check cache
    cache_key = _listening_history_cache_key(username, track, artist, max_months)
    with LISTENING_HISTORY_CACHE_LOCK:
        cached = LISTENING_HISTORY_CACHE.get(cache_key)
        if cached and time.time() - cached["ts"] < LISTENING_HISTORY_CACHE_TTL_SECONDS:
            return jsonify(cached["data"])

    try:
        chart_list_data = lastfm_get("user.getWeeklyChartList", user=username)
    except Exception:
        return jsonify({"error": "Failed to fetch chart list from Last.fm"}), 502

    charts = chart_list_data.get("weeklychartlist", {}).get("chart", [])
    if not charts:
        return jsonify([])

    now = datetime.now(timezone.utc)

    # Build the list of expected months: current month + (max_months - 1) prior
    expected_months: list[str] = []
    for i in range(max_months):
        dt = now.replace(day=1) - timedelta(days=i * 28)
        expected_months.append(dt.strftime("%Y-%m"))
    expected_months = sorted(set(expected_months))

    cutoff_dt = datetime.strptime(expected_months[0], "%Y-%m").replace(tzinfo=timezone.utc)
    cutoff_ts = int(cutoff_dt.timestamp())

    # Group chart weeks into calendar months
    monthly_weeks: dict[str, list[dict]] = {m: [] for m in expected_months}
    for chart in charts:
        from_ts = int(chart.get("from", 0))
        if from_ts < cutoff_ts:
            continue
        month_key = datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m")
        monthly_weeks.setdefault(month_key, []).append(chart)

    norm_track = normalize_lastfm_text(track)
    norm_artist = normalize_lastfm_text(artist)

    # Flatten all weeks across months for parallel fetching
    week_jobs: list[tuple[str, dict]] = []
    for month_key in sorted(monthly_weeks.keys()):
        for week in monthly_weeks[month_key]:
            week_jobs.append((month_key, week))

    # Fetch weekly charts in parallel
    month_plays: dict[str, int] = {mk: 0 for mk in monthly_weeks}
    with ThreadPoolExecutor(max_workers=LISTENING_HISTORY_MAX_WORKERS) as pool:
        future_to_month = {
            pool.submit(_fetch_week_plays, username, week, norm_track, norm_artist): month_key
            for month_key, week in week_jobs
        }
        for future in as_completed(future_to_month):
            month_key = future_to_month[future]
            try:
                month_plays[month_key] += future.result()
            except Exception:
                pass

    # Supplement with recent tracks for the current incomplete week.
    # Weekly charts only cover completed weeks, so plays from the current
    # week (including today) would otherwise be missing.
    last_chart_to = int(charts[-1].get("to", 0)) if charts else 0
    current_month_key = now.strftime("%Y-%m")
    try:
        recent_data = lastfm_get(
            "user.getRecentTracks",
            user=username,
            limit=200,
            **{"from": str(last_chart_to), "to": str(int(now.timestamp()))},
        )
        recent_tracks = recent_data.get("recenttracks", {}).get("track", [])
        if isinstance(recent_tracks, dict):
            recent_tracks = [recent_tracks]
        for rt in recent_tracks:
            # Skip the "now playing" entry (has @attr.nowplaying but no date)
            if rt.get("@attr", {}).get("nowplaying"):
                continue
            rt_name = normalize_lastfm_text(rt.get("name", ""))
            rt_artist = normalize_lastfm_text(extract_artist_name(rt.get("artist")))
            if rt_name == norm_track and rt_artist == norm_artist:
                # Determine which month this scrobble belongs to
                rt_ts = int(rt.get("date", {}).get("uts", 0))
                if rt_ts:
                    rt_month = datetime.fromtimestamp(rt_ts, tz=timezone.utc).strftime("%Y-%m")
                else:
                    rt_month = current_month_key
                if rt_month in month_plays:
                    month_plays[rt_month] += 1
    except Exception:
        pass  # Best-effort; chart data is still valid

    result = []
    for month_key in expected_months:
        dt = datetime.strptime(month_key, "%Y-%m")
        result.append({
            "month": month_key,
            "label": dt.strftime("%b %Y"),
            "plays": month_plays.get(month_key, 0),
        })

    # Store in cache
    with LISTENING_HISTORY_CACHE_LOCK:
        LISTENING_HISTORY_CACHE[cache_key] = {"data": result, "ts": time.time()}

    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
