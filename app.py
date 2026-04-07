import os
import re
import time
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

with app.app_context():
    db.init_db()

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
LIBRARY_PAGE_SIZE = 50
TRACK_PAGE_DATE_RE = re.compile(
    r'<span title="(?:[A-Z][a-z]+ )?([0-9]{1,2} [A-Z][a-z]{2} [0-9]{4}, [0-9]{1,2}:[0-9]{2}(?:am|pm))">'
)
TRACK_PAGE_PAGINATION_RE = re.compile(r'href="\?page=(\d+)"')


# Last.fm returns this hash for the default "star" placeholder — treat as no image
LASTFM_PLACEHOLDER_HASH = "2a96cbd8b46e442fc41c2b86b821562f"


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


def public_library_first_listen_date(
    username: str, artist: str, track: str, total_scrobbles: int
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

    resp = requests.get(base_url, headers=headers, timeout=20)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    page_count = max(
        [int(m) for m in TRACK_PAGE_PAGINATION_RE.findall(resp.text)] or [1]
    )
    if total_scrobbles > 0:
        page_count = max(page_count, ceil(total_scrobbles / LIBRARY_PAGE_SIZE))

    last_page_html = resp.text
    if page_count > 1:
        last_resp = requests.get(
            f"{base_url}?page={page_count}", headers=headers, timeout=20
        )
        last_resp.raise_for_status()
        last_page_html = last_resp.text

    matches = TRACK_PAGE_DATE_RE.findall(last_page_html)
    if not matches:
        return None

    return matches[-1]


def lastfm_library_date_to_timestamp(date_text: str) -> str:
    """Convert a Last.fm library page date to a unix timestamp string."""
    dt = datetime.strptime(date_text, "%d %b %Y, %I:%M%p")
    dt = dt.replace(tzinfo=ZoneInfo("Europe/Vienna"))
    return str(int(dt.timestamp()))


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
        data = lastfm_get("user.getTopTracks", user=username, period=period, limit=8)
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

            # Sort by plays descending, take top 5
            track_list.sort(key=lambda x: x["plays"], reverse=True)
            top = track_list[:5]

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


@app.route("/api/first-listen")
def first_listen():
    """Find the very first scrobble of a track for the given user."""
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    username = request.args.get("username", "").strip()
    if not track or not artist or not username:
        return jsonify({"error": "track, artist, and username are required"}), 400

    # Return cached result immediately if available (first-listen date never changes)
    # Skip cache entries that have no date — those are transient failures worth retrying.
    cached = db.get_cached(username, track, artist)
    if cached and cached.get("first_listen_date"):
        cached_timestamp = cached["first_listen_timestamp"] or ""
        cached_date = cached["first_listen_date"] or ""
        date_unavailable = not bool(cached_date)
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
            }
        )

    # Step 1: Check total play count via track.getInfo (fast, single call)
    try:
        info = lastfm_get(
            "track.getInfo", track=track, artist=artist, username=username
        )
    except requests.HTTPError:
        return jsonify({"error": "Last.fm API error"}), 502

    track_info = info.get("track", {})
    total = int(track_info.get("userplaycount", 0))

    if total == 0:
        return jsonify(
            {
                "found": False,
                "track": track,
                "artist": artist,
                "message": "You have never listened to this track.",
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
    exact_date = None
    exact_ts = ""
    date_unavailable_reason = ""

    try:
        exact_date = public_library_first_listen_date(
            username, canonical_artist, canonical_track, total
        )
        if exact_date:
            exact_ts = lastfm_library_date_to_timestamp(exact_date)
    except requests.RequestException:
        date_unavailable_reason = (
            "The public Last.fm track page could not be fetched, so the exact first-listen timestamp could not be determined."
        )
    except ValueError:
        date_unavailable_reason = (
            "The public Last.fm track page exposed a date, but it could not be converted into a timestamp."
        )

    if not exact_date:
        date_unavailable_reason = date_unavailable_reason or (
            "Last.fm reports a play for this track, but the public track history page does not expose an exact first-listen timestamp."
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
