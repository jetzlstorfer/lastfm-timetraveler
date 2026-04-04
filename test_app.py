"""Tests for the Last.fm Time Traveler app."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

import database
import app as app_module
from app import app


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Point the database module at a fresh temp file for every test."""
    db_file = str(tmp_path / "test.db")
    with patch.object(database, "DB_PATH", db_file):
        database.init_db()
        yield db_file


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


# ---------------------------------------------------------------------------
# Database caching
# ---------------------------------------------------------------------------

class TestDatabaseCaching:
    @patch.object(app_module, "LASTFM_USERNAME", "testuser")
    @patch.object(app_module, "lastfm_get")
    def test_result_is_cached_after_first_query(self, mock_get, client):
        """After a successful lookup the result must be stored in the DB."""
        weeks = [(1000000, 1604800)]

        def fake_lastfm_get(method, **kwargs):
            if method == "track.getInfo":
                return _track_info_response(userplaycount=5, track="Cached Song", artist="Artist")
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list(weeks)
            if method == "user.getWeeklyTrackChart":
                return _weekly_track_chart([("Cached Song", "Artist", 5)])
            if method == "user.getRecentTracks":
                return _recent_tracks_response(
                    [("Cached Song", "Artist", "10 Jan 2007, 09:00", "1168419600")],
                    page=1, total_pages=1,
                )
            return {}

        mock_get.side_effect = fake_lastfm_get
        resp = client.get("/api/first-listen?track=Cached+Song&artist=Artist")
        data = resp.get_json()
        assert data["found"] is True
        assert data.get("cached") is None  # first hit is not from cache

        # Verify it was stored
        stored = database.get_cached("testuser", "Cached Song", "Artist")
        assert stored is not None
        assert stored["track"] == "Cached Song"
        assert stored["first_listen_date"] == "10 Jan 2007, 09:00"

    @patch.object(app_module, "LASTFM_USERNAME", "testuser")
    @patch.object(app_module, "lastfm_get")
    def test_cached_result_served_without_api_call(self, mock_get, client):
        """Second query for the same track must come from cache (no API call)."""
        # Pre-populate cache
        database.save_result(
            "testuser", "Lazy Song", "Bruno Mars",
            "Doo-Wops & Hooligans",
            "01 May 2011, 14:00", "1304258400",
            88, "https://img.example/lazy.jpg",
        )

        resp = client.get("/api/first-listen?track=Lazy+Song&artist=Bruno+Mars")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        assert data["track"] == "Lazy Song"
        assert data["date"] == "01 May 2011, 14:00"
        assert data["total_scrobbles"] == 88
        # The Last.fm API must not have been called at all
        mock_get.assert_not_called()

    @patch.object(app_module, "LASTFM_USERNAME", "testuser")
    @patch.object(app_module, "lastfm_get")
    def test_cache_lookup_is_case_insensitive(self, mock_get, client):
        """Cache hit should work regardless of the casing used in the query."""
        database.save_result(
            "testuser", "Bohemian Rhapsody", "Queen",
            "A Night at the Opera",
            "15 Mar 2005, 20:00", "1110924000",
            1000, "",
        )

        resp = client.get("/api/first-listen?track=bohemian+rhapsody&artist=queen")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# /api/history
# ---------------------------------------------------------------------------

class TestHistory:
    @patch.object(app_module, "LASTFM_USERNAME", "testuser")
    def test_empty_history(self, client):
        resp = client.get("/api/history")
        assert resp.status_code == 200
        assert resp.get_json() == []

    @patch.object(app_module, "LASTFM_USERNAME", "testuser")
    def test_history_returns_saved_results(self, client):
        database.save_result(
            "testuser", "Track One", "Artist A", "Album A",
            "01 Jan 2010, 12:00", "1262347200", 10, "",
        )
        database.save_result(
            "testuser", "Track Two", "Artist B", "Album B",
            "15 Jun 2015, 18:30", "1434389400", 25, "",
        )

        resp = client.get("/api/history")
        data = resp.get_json()

        assert len(data) == 2
        tracks = {r["track"] for r in data}
        assert tracks == {"Track One", "Track Two"}

    @patch.object(app_module, "LASTFM_USERNAME", "testuser")
    def test_history_filters_by_username(self, client):
        database.save_result(
            "testuser", "My Track", "My Artist", "", "01 Jan 2010, 00:00", "1262304000", 5, "",
        )
        database.save_result(
            "otheruser", "Other Track", "Other Artist", "", "01 Jan 2012, 00:00", "1325376000", 3, "",
        )

        resp = client.get("/api/history?username=testuser")
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["track"] == "My Track"

        resp2 = client.get("/api/history?username=otheruser")
        data2 = resp2.get_json()
        assert len(data2) == 1
        assert data2[0]["track"] == "Other Track"

