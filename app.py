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
            }
        )
    return jsonify(results)


@app.route("/api/first-listen")
def first_listen():
    """Find the very first scrobble of a track for the configured user."""
    track = request.args.get("track", "").strip()
    artist = request.args.get("artist", "").strip()
    if not track or not artist:
        return jsonify({"error": "track and artist are required"}), 400

    # user.getArtistTracks returns scrobbles of a given artist,
    # filterable by track name, ordered by date.
    # We request page 1 first to learn total pages, then fetch the last page
    # (oldest scrobbles) to find the very first listen.
    try:
        first_page = lastfm_get(
            "user.getArtistTracks",
            user=LASTFM_USERNAME,
            artist=artist,
            track=track,
            page=1,
        )
    except requests.HTTPError:
        return jsonify({"error": "Last.fm API error"}), 502

    tracks_data = first_page.get("artisttracks", {})
    attrs = tracks_data.get("@attr", {})
    total_pages = int(attrs.get("totalPages", 0))
    total = int(attrs.get("total", 0))

    if total == 0:
        return jsonify(
            {
                "found": False,
                "track": track,
                "artist": artist,
                "message": "You have never listened to this track.",
            }
        )

    # Fetch the last page (oldest scrobbles first because API returns newest first)
    if total_pages > 1:
        last_page = lastfm_get(
            "user.getArtistTracks",
            user=LASTFM_USERNAME,
            artist=artist,
            track=track,
            page=total_pages,
        )
        page_tracks = last_page.get("artisttracks", {}).get("track", [])
    else:
        page_tracks = tracks_data.get("track", [])

    if isinstance(page_tracks, dict):
        page_tracks = [page_tracks]

    if not page_tracks:
        return jsonify({"found": False, "track": track, "artist": artist})

    # The last entry on the last page is the oldest scrobble
    oldest = page_tracks[-1]
    date_info = oldest.get("date", {})

    # Fetch track info for album art and album name
    image_url = ""
    album_name = ""
    try:
        info = lastfm_get("track.getInfo", track=track, artist=artist)
        album_data = info.get("track", {}).get("album", {})
        album_name = album_data.get("title", "")
        for img in album_data.get("image", []):
            if img.get("size") == "extralarge" and img.get("#text"):
                image_url = img["#text"]
    except Exception:
        pass

    return jsonify(
        {
            "found": True,
            "track": oldest.get("name", track),
            "artist": oldest.get("artist", {}).get("#text", artist)
            if isinstance(oldest.get("artist"), dict)
            else artist,
            "album": album_name,
            "date": date_info.get("#text", "Unknown"),
            "timestamp": date_info.get("uts", ""),
            "total_scrobbles": total,
            "image": image_url,
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
