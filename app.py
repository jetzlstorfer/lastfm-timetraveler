import os
from datetime import datetime, timezone, timedelta
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


def lastfm_get(method: str, **params):
    params.update({"method": method, "api_key": LASTFM_API_KEY, "format": "json"})
    resp = requests.get(LASTFM_BASE, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


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
            if img.get("size") == "medium" and img.get("#text"):
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
            image_url = ""
            for img in t.get("image", []):
                if img.get("size") == "medium" and img.get("#text"):
                    image_url = img["#text"]
            results.append({
                "name": t.get("name", ""),
                "artist": t.get("artist", {}).get("name", ""),
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
                        if img.get("size") == "medium" and img.get("#text"):
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
            if img.get("size") == "medium" and img.get("#text"):
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


def _track_in_chart_range(username, from_ts, to_ts, track, artist):
    """Check if a track appears in the user's weekly chart for a time range."""
    data = lastfm_get(
        "user.getWeeklyTrackChart",
        user=username,
        **{"from": str(from_ts), "to": str(to_ts)},
    )
    tracks = data.get("weeklytrackchart", {}).get("track", [])
    if isinstance(tracks, dict):
        tracks = [tracks]
    track_l, artist_l = track.lower(), artist.lower()
    for t in tracks:
        t_artist = t.get("artist", {})
        t_artist_name = (
            t_artist.get("#text", "") if isinstance(t_artist, dict) else str(t_artist)
        )
        if t.get("name", "").lower() == track_l and t_artist_name.lower() == artist_l:
            return True
    return False


@app.route("/api/first-listen")
def first_listen():
    """Find the very first scrobble of a track for the given user."""
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    username = request.args.get("username", "").strip()
    if not track or not artist or not username:
        return jsonify({"error": "track, artist, and username are required"}), 400

    # Return cached result immediately if available (first-listen date never changes)
    if LASTFM_USERNAME:
        cached = db.get_cached(LASTFM_USERNAME, track, artist)
        if cached:
            return jsonify(
                {
                    "found": True,
                    "track": cached["track"],
                    "artist": cached["artist"],
                    "album": cached["album"] or "",
                    "date": cached["first_listen_date"],
                    "timestamp": cached["first_listen_timestamp"],
                    "total_scrobbles": cached["total_scrobbles"],
                    "image": cached["image"] or "",
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
        if img.get("size") == "extralarge" and img.get("#text"):
            image_url = img["#text"]

    # Canonical names from Last.fm
    canonical_track = track_info.get("name", track)
    canonical_artist = track_info.get("artist", {}).get("name", artist)

    # Step 2: Binary search weekly charts to find the earliest week
    try:
        chart_list = lastfm_get("user.getWeeklyChartList", user=username)
    except requests.HTTPError:
        return jsonify({"error": "Last.fm API error"}), 502

    weeks = chart_list.get("weeklychartlist", {}).get("chart", [])
    if not weeks:
        return jsonify({"error": "Could not retrieve chart history"}), 502

    first_from = int(weeks[0]["from"])
    lo, hi = 0, len(weeks) - 1

    while lo < hi:
        mid = (lo + hi) // 2
        mid_to = int(weeks[mid]["to"])
        if _track_in_chart_range(username, first_from, mid_to, canonical_track, canonical_artist):
            hi = mid
        else:
            lo = mid + 1

    # lo == hi == index of the earliest week containing the track
    earliest_week = weeks[lo]
    week_from = int(earliest_week["from"])
    week_to = int(earliest_week["to"])

    # Step 3: Try to find the exact scrobble date within that week
    exact_date = None
    exact_ts = None
    try:
        page_data = lastfm_get(
            "user.getRecentTracks",
            user=username,
            limit=200,
            **{"from": str(week_from), "to": str(week_to)},
        )
        total_pages = int(
            page_data.get("recenttracks", {}).get("@attr", {}).get("totalPages", 0)
        )
        track_l = canonical_track.lower()
        artist_l = canonical_artist.lower()

        # Scan from the last page (oldest scrobbles) to find the first occurrence
        for page in range(total_pages, 0, -1):
            if page == 1 and total_pages == 1:
                scrobbles = page_data.get("recenttracks", {}).get("track", [])
            else:
                pd = lastfm_get(
                    "user.getRecentTracks",
                    user=username,
                    limit=200,
                    page=page,
                    **{"from": str(week_from), "to": str(week_to)},
                )
                scrobbles = pd.get("recenttracks", {}).get("track", [])
            if isinstance(scrobbles, dict):
                scrobbles = [scrobbles]
            for s in reversed(scrobbles):
                s_artist = s.get("artist", {})
                s_artist_name = (
                    s_artist.get("#text", "")
                    if isinstance(s_artist, dict)
                    else str(s_artist)
                )
                if (
                    s.get("name", "").lower() == track_l
                    and s_artist_name.lower() == artist_l
                ):
                    date_info = s.get("date", {})
                    exact_date = date_info.get("#text")
                    exact_ts = date_info.get("uts")
                    break
            if exact_date:
                break
    except Exception:
        pass

    # Fall back to week start date if exact date not found
    if not exact_date:
        dt = datetime.fromtimestamp(week_from, tz=timezone.utc)
        exact_date = dt.strftime("%d %b %Y, %H:%M")
        exact_ts = str(week_from)

    # Persist the result so future queries are served from the local cache
    if LASTFM_USERNAME:
        db.save_result(
            LASTFM_USERNAME,
            canonical_track,
            canonical_artist,
            album_name,
            exact_date,
            exact_ts,
            total,
            image_url,
        )

    return jsonify(
        {
            "found": True,
            "track": canonical_track,
            "artist": canonical_artist,
            "album": album_name,
            "date": exact_date,
            "timestamp": exact_ts,
            "total_scrobbles": total,
            "image": image_url,
        }
    )


@app.route("/api/history")
def history():
    """Return all previously resolved first-listen results for the configured user."""
    username = request.args.get("username", LASTFM_USERNAME)
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
