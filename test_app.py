"""Tests for the Last.fm Time Traveler app."""

import json
from unittest.mock import patch

import pytest

import app as app_module
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers to build fake Last.fm API responses
# ---------------------------------------------------------------------------

def _track_info_response(userplaycount, track="Songname", artist="Artistname",
                         album="Albumname", image_url="https://img.example/art.jpg"):
    """Fake response for track.getInfo."""
    return {
        "track": {
            "name": track,
            "artist": {"name": artist},
            "userplaycount": str(userplaycount),
            "userloved": "0",
            "album": {
                "title": album,
                "image": [
                    {"size": "small", "#text": ""},
                    {"size": "extralarge", "#text": image_url},
                ],
            },
        }
    }


def _weekly_chart_list(weeks):
    """Fake response for user.getWeeklyChartList.

    *weeks* is a list of (from_ts, to_ts) tuples.
    """
    return {
        "weeklychartlist": {
            "chart": [
                {"#text": "", "from": str(f), "to": str(t)} for f, t in weeks
            ]
        }
    }


def _weekly_track_chart(tracks):
    """Fake response for user.getWeeklyTrackChart.

    *tracks* is a list of (name, artist, playcount) tuples.
    """
    return {
        "weeklytrackchart": {
            "track": [
                {
                    "name": name,
                    "artist": {"#text": artist},
                    "playcount": str(pc),
                }
                for name, artist, pc in tracks
            ]
        }
    }


def _recent_tracks_response(scrobbles, page=1, total_pages=1):
    """Fake response for user.getRecentTracks.

    *scrobbles* is a list of (name, artist, date_text, date_uts) tuples.
    """
    return {
        "recenttracks": {
            "@attr": {
                "page": str(page),
                "totalPages": str(total_pages),
                "total": str(len(scrobbles)),
            },
            "track": [
                {
                    "name": name,
                    "artist": {"#text": artist},
                    "date": {"#text": date_text, "uts": date_uts},
                }
                for name, artist, date_text, date_uts in scrobbles
            ],
        }
    }


def _user_info_response(name="testuser"):
    return {"user": {"name": name}}


def _search_response(tracks):
    """Fake response for track.search.

    *tracks* is a list of (name, artist, listeners) tuples.
    """
    return {
        "results": {
            "trackmatches": {
                "track": [
                    {
                        "name": name,
                        "artist": artist,
                        "listeners": str(listeners),
                        "image": [],
                    }
                    for name, artist, listeners in tracks
                ]
            }
        }
    }


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

class TestStatus:
    @patch.object(app_module, "LASTFM_API_KEY", "valid_key")
    @patch.object(app_module, "LASTFM_USERNAME", "testuser")
    @patch.object(app_module, "lastfm_get", return_value=_user_info_response("testuser"))
    def test_status_ok(self, mock_get, client):
        resp = client.get("/api/status")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["username"] == "testuser"

    @patch.object(app_module, "LASTFM_API_KEY", "")
    def test_status_missing_api_key(self, client):
        resp = client.get("/api/status")
        data = resp.get_json()
        assert data["ok"] is False
        assert "LASTFM_API_KEY" in data["error"]

    @patch.object(app_module, "LASTFM_API_KEY", "valid_key")
    @patch.object(app_module, "LASTFM_USERNAME", "")
    def test_status_missing_username(self, client):
        resp = client.get("/api/status")
        data = resp.get_json()
        assert data["ok"] is False
        assert "LASTFM_USERNAME" in data["error"]


# ---------------------------------------------------------------------------
# /api/search
# ---------------------------------------------------------------------------

class TestSearch:
    @patch.object(app_module, "lastfm_get")
    def test_search_returns_sorted_results(self, mock_get, client):
        mock_get.return_value = _search_response([
            ("Song A", "Artist A", 100),
            ("Song B", "Artist B", 5000),
            ("Song C", "Artist C", 500),
        ])
        resp = client.get("/api/search?q=song")
        data = resp.get_json()
        assert len(data) == 3
        assert data[0]["name"] == "Song B"  # highest listeners first
        assert data[1]["name"] == "Song C"
        assert data[2]["name"] == "Song A"

    def test_search_short_query_returns_empty(self, client):
        resp = client.get("/api/search?q=a")
        assert resp.get_json() == []

    def test_search_empty_query_returns_empty(self, client):
        resp = client.get("/api/search?q=")
        assert resp.get_json() == []


# ---------------------------------------------------------------------------
# /api/first-listen
# ---------------------------------------------------------------------------

class TestFirstListen:
    def test_missing_params_returns_400(self, client):
        resp = client.get("/api/first-listen")
        assert resp.status_code == 400

        resp = client.get("/api/first-listen?track=Hello")
        assert resp.status_code == 400

        resp = client.get("/api/first-listen?artist=Adele")
        assert resp.status_code == 400

    @patch.object(app_module, "lastfm_get")
    def test_never_listened_track(self, mock_get, client):
        mock_get.return_value = _track_info_response(userplaycount=0)
        resp = client.get("/api/first-listen?track=Unknown&artist=Nobody")
        data = resp.get_json()
        assert data["found"] is False
        assert "never" in data["message"].lower()

    @patch.object(app_module, "lastfm_get")
    def test_track_found_with_exact_date(self, mock_get, client):
        """Full happy path: binary search finds the week, getRecentTracks finds the exact date."""
        weeks = [(1000000 + i * 604800, 1000000 + (i + 1) * 604800) for i in range(20)]

        def fake_lastfm_get(method, **kwargs):
            if method == "track.getInfo":
                return _track_info_response(
                    userplaycount=42, track="TestTrack", artist="TestArtist",
                    album="TestAlbum", image_url="https://img.example/cover.jpg",
                )
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list(weeks)
            if method == "user.getWeeklyTrackChart":
                to_ts = int(kwargs.get("to", 0))
                # Track first appears in week index 10
                target_to = weeks[10][1]
                if to_ts >= target_to:
                    return _weekly_track_chart([("TestTrack", "TestArtist", 3)])
                return _weekly_track_chart([])
            if method == "user.getRecentTracks":
                return _recent_tracks_response([
                    ("OtherSong", "OtherArtist", "01 Jan 2007, 15:00", "1167663600"),
                    ("TestTrack", "TestArtist", "02 Jan 2007, 20:30", "1167769800"),
                ], page=1, total_pages=1)
            return {}

        mock_get.side_effect = fake_lastfm_get
        resp = client.get("/api/first-listen?track=TestTrack&artist=TestArtist")
        data = resp.get_json()

        assert data["found"] is True
        assert data["track"] == "TestTrack"
        assert data["artist"] == "TestArtist"
        assert data["album"] == "TestAlbum"
        assert data["total_scrobbles"] == 42
        assert data["date"] == "02 Jan 2007, 20:30"
        assert data["timestamp"] == "1167769800"
        assert data["image"] == "https://img.example/cover.jpg"

    @patch.object(app_module, "lastfm_get")
    def test_fallback_to_week_start_when_exact_date_unavailable(self, mock_get, client):
        """When getRecentTracks doesn't find the track, falls back to week start."""
        weeks = [(1167609600, 1168214400), (1168214400, 1168819200)]

        def fake_lastfm_get(method, **kwargs):
            if method == "track.getInfo":
                return _track_info_response(userplaycount=5, track="Song", artist="Band")
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list(weeks)
            if method == "user.getWeeklyTrackChart":
                # Track in all weeks
                return _weekly_track_chart([("Song", "Band", 5)])
            if method == "user.getRecentTracks":
                # Return empty — exact date unavailable (old data)
                return _recent_tracks_response([], page=1, total_pages=0)
            return {}

        mock_get.side_effect = fake_lastfm_get
        resp = client.get("/api/first-listen?track=Song&artist=Band")
        data = resp.get_json()

        assert data["found"] is True
        # Falls back to the start of the earliest week
        assert data["timestamp"] == str(weeks[0][0])

    @patch.object(app_module, "lastfm_get")
    def test_binary_search_converges_to_correct_week(self, mock_get, client):
        """Verify the binary search picks the earliest week, not just any matching one."""
        weeks = [(1000000 + i * 604800, 1000000 + (i + 1) * 604800) for i in range(100)]
        first_week_idx = 37  # track first appears in week 37

        def fake_lastfm_get(method, **kwargs):
            if method == "track.getInfo":
                return _track_info_response(userplaycount=10, track="X", artist="Y")
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list(weeks)
            if method == "user.getWeeklyTrackChart":
                to_ts = int(kwargs.get("to", 0))
                target_to = weeks[first_week_idx][1]
                if to_ts >= target_to:
                    return _weekly_track_chart([("X", "Y", 1)])
                return _weekly_track_chart([])
            if method == "user.getRecentTracks":
                return _recent_tracks_response([], page=1, total_pages=0)
            return {}

        mock_get.side_effect = fake_lastfm_get
        resp = client.get("/api/first-listen?track=X&artist=Y")
        data = resp.get_json()

        assert data["found"] is True
        # Should fall back to the start of week 37
        assert data["timestamp"] == str(weeks[first_week_idx][0])

    @patch.object(app_module, "lastfm_get")
    def test_track_getinfo_http_error_returns_502(self, mock_get, client):
        import requests as req
        mock_get.side_effect = req.HTTPError("503 Server Error")
        resp = client.get("/api/first-listen?track=A&artist=B")
        assert resp.status_code == 502

    @patch.object(app_module, "lastfm_get")
    def test_single_week_history(self, mock_get, client):
        """User with only one week of scrobbling history."""
        weeks = [(1167609600, 1168214400)]

        def fake_lastfm_get(method, **kwargs):
            if method == "track.getInfo":
                return _track_info_response(userplaycount=1, track="Only", artist="One")
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list(weeks)
            if method == "user.getWeeklyTrackChart":
                return _weekly_track_chart([("Only", "One", 1)])
            if method == "user.getRecentTracks":
                return _recent_tracks_response(
                    [("Only", "One", "03 Jan 2007, 10:00", "1167818400")],
                    page=1, total_pages=1,
                )
            return {}

        mock_get.side_effect = fake_lastfm_get
        resp = client.get("/api/first-listen?track=Only&artist=One")
        data = resp.get_json()

        assert data["found"] is True
        assert data["date"] == "03 Jan 2007, 10:00"
        assert data["total_scrobbles"] == 1

    @patch.object(app_module, "lastfm_get")
    def test_case_insensitive_matching(self, mock_get, client):
        """Track/artist matching in weekly charts should be case-insensitive."""
        weeks = [(1000000, 1604800)]

        def fake_lastfm_get(method, **kwargs):
            if method == "track.getInfo":
                return _track_info_response(userplaycount=3, track="My Song", artist="The Band")
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list(weeks)
            if method == "user.getWeeklyTrackChart":
                # API returns lowercase
                return _weekly_track_chart([("my song", "the band", 3)])
            if method == "user.getRecentTracks":
                return _recent_tracks_response(
                    [("my song", "the band", "15 Jan 2007, 12:00", "1168855200")],
                    page=1, total_pages=1,
                )
            return {}

        mock_get.side_effect = fake_lastfm_get
        resp = client.get("/api/first-listen?track=My+Song&artist=The+Band")
        data = resp.get_json()
        assert data["found"] is True
