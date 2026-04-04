import os
import requests
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static")

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_USERNAME = os.getenv("LASTFM_USERNAME")
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
    """Health check — verifies API key and username are configured and valid."""
    if not LASTFM_API_KEY or LASTFM_API_KEY == "your_api_key_here":
        return jsonify({"ok": False, "error": "LASTFM_API_KEY is not set. Copy .env.example to .env and add your key."}), 200
    if not LASTFM_USERNAME or LASTFM_USERNAME == "your_lastfm_username_here":
        return jsonify({"ok": False, "error": "LASTFM_USERNAME is not set. Add your Last.fm username to .env."}), 200
    try:
        data = lastfm_get("user.getInfo", user=LASTFM_USERNAME)
        user = data.get("user", {}).get("name", LASTFM_USERNAME)
        return jsonify({"ok": True, "username": user})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Last.fm API error: {exc}"}), 200


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


def _track_in_chart_range(from_ts, to_ts, track, artist):
    """Check if a track appears in the user's weekly chart for a time range."""
    data = lastfm_get(
        "user.getWeeklyTrackChart",
        user=LASTFM_USERNAME,
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
    """Find the very first scrobble of a track for the configured user."""
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    if not track or not artist:
        return jsonify({"error": "track and artist are required"}), 400

    # Step 1: Check total play count via track.getInfo (fast, single call)
    try:
        info = lastfm_get(
            "track.getInfo", track=track, artist=artist, username=LASTFM_USERNAME
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
        chart_list = lastfm_get("user.getWeeklyChartList", user=LASTFM_USERNAME)
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
        if _track_in_chart_range(first_from, mid_to, canonical_track, canonical_artist):
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
            user=LASTFM_USERNAME,
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
                    user=LASTFM_USERNAME,
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
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(week_from, tz=timezone.utc)
        exact_date = dt.strftime("%d %b %Y, %H:%M")
        exact_ts = str(week_from)

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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
