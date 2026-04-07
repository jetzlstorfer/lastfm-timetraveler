import os
import re
import time
import logging
from threading import Lock
from math import ceil
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
import database as db

load_dotenv()

app = Flask(__name__, static_folder="static")
app.logger.setLevel(logging.INFO)

with app.app_context():
    db.init_db()

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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def status():
    """Health check — verifies API key is configured."""
    if not LASTFM_API_KEY or LASTFM_API_KEY == "your_api_key_here":
        return jsonify({"ok": False, "error": "LASTFM_API_KEY is not set. Copy .env.example to .env and add your key."}), 200
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


@app.route("/api/first-listen")
def first_listen():
    """Find the very first scrobble of a track for the given user."""
    started_at = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started_at) * 1000)

    lookup_id = request.args.get("lookup_id", "").strip() or None
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    username = request.args.get("username", "").strip()
    if track and artist and username:
        app.logger.info("lookup request started %s", lookup_context(username, artist, track))
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
        finish_lookup_progress(
            lookup_id,
            username=username,
            artist=cached["artist"],
            track=cached["track"],
            stage="cache-hit",
            status="Loaded from cache",
            detail="This lookup was already cached locally, so no page scan was needed.",
            pages_checked=1,
            pages_total=1,
        )
        return jsonify(
            {
                "found": True,
                "track": cached["track"],
                "artist": cached["artist"],
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
            }
        )

    # Step 1: Check total play count via track.getInfo (fast, single call)
    try:
        info = lastfm_get(
            "track.getInfo", track=track, artist=artist, username=username
        )
    except requests.HTTPError:
        finish_lookup_progress(
            lookup_id,
            username=username,
            artist=artist,
            track=track,
            stage="request-error",
            status="Lookup failed",
            detail="Last.fm returned an API error while loading track metadata.",
        )
        return jsonify({"error": "Last.fm API error", "elapsed_ms": elapsed_ms()}), 502

    track_info = info.get("track", {})
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
        )
        return jsonify(
            {
                "found": False,
                "track": track,
                "artist": artist,
                "message": "You have never listened to this track.",
                "cached": False,
                "elapsed_ms": elapsed_ms(),
            }
        )

    # Gather album art / album name from the same track.getInfo response
    image_url = ""
    album_name = ""
    album_data = track_info.get("album", {})
    album_name = album_data.get("title", "")
    for img in album_data.get("image", []):
        if img.get("size") == "extralarge" and img.get("#text") and not is_placeholder(img["#text"]):
            image_url = img["#text"]

    # Canonical names from Last.fm
    canonical_track = track_info.get("name", track)
    canonical_artist = track_info.get("artist", {}).get("name", artist)
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
    )

    return jsonify(
        {
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
        }
    )


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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
