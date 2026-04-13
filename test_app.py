"""Tests for the Last.fm Time Traveler app."""

import json
import os
import sqlite3
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import unquote

import pytest

import database
import app as app_module
from app import app


def _await_first_listen(client, query_string, timeout=10):
    """Fire a first-listen request and, if async (202), poll until the result is ready.

    Returns ``(response_data_dict, http_status_code_of_initial_request)``.
    """
    resp = client.get(f"/api/first-listen?{query_string}")
    data = resp.get_json()

    if resp.status_code != 202:
        return data, resp.status_code

    lookup_id = data["lookup_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.05)
        prog_resp = client.get(f"/api/lookup-progress?lookup_id={lookup_id}")
        prog = prog_resp.get_json()
        if prog.get("result") is not None:
            return prog["result"], 200
    raise TimeoutError(f"Lookup {lookup_id} did not complete within {timeout}s")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Point the database module at a fresh temp file for every test."""
    db_file = str(tmp_path / "test.db")
    with patch.object(database, "DB_PATH", db_file):
        database.init_db()
        with app_module.LOOKUP_PROGRESS_LOCK:
            app_module.LOOKUP_PROGRESS.clear()
        with app_module.LISTENING_HISTORY_CACHE_LOCK:
            app_module.LISTENING_HISTORY_CACHE.clear()
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
    response = {
        "track": {
            "name": track,
            "artist": {"name": artist},
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

    if userplaycount is not None:
        response["track"]["userplaycount"] = str(userplaycount)
    return response
    return response


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
    def test_status_ok(self, client):
        resp = client.get("/api/status")
        data = resp.get_json()
        assert data["ok"] is True

    @patch.object(app_module, "LASTFM_API_KEY", "")
    def test_status_missing_api_key(self, client):
        resp = client.get("/api/status")
        data = resp.get_json()
        assert data["ok"] is False
        assert "LASTFM_API_KEY" in data["error"]


class TestReadiness:
    @patch.object(app_module, "LASTFM_API_KEY", "valid_key")
    def test_ready_ok(self, client):
        resp = client.get("/api/ready")
        data = resp.get_json()

        assert resp.status_code == 200
        assert data["ok"] is True

    @patch.object(app_module, "LASTFM_API_KEY", "")
    def test_ready_missing_api_key(self, client):
        resp = client.get("/api/ready")
        data = resp.get_json()

        assert resp.status_code == 503
        assert data["ok"] is False

    @patch.object(app_module, "LASTFM_API_KEY", "valid_key")
    @patch.object(database, "init_db", side_effect=sqlite3.OperationalError("database is locked"))
    def test_ready_returns_503_when_database_unavailable(self, _mock_init_db, client):
        resp = client.get("/api/ready")
        data = resp.get_json()

        assert resp.status_code == 503
        assert data["ok"] is False
        assert "Database is not ready" in data["error"]


class TestDatabaseInitialization:
    def test_init_db_waits_for_transient_lock(self, tmp_path):
        db_file = str(tmp_path / "startup-lock.db")
        lock_acquired = threading.Event()
        release_lock = threading.Event()
        init_completed = threading.Event()
        init_errors = []

        def hold_exclusive_lock():
            with sqlite3.connect(db_file, timeout=1) as conn:
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute("CREATE TABLE IF NOT EXISTS startup_lock (id INTEGER)")
                lock_acquired.set()
                release_lock.wait(timeout=5)
                conn.commit()

        def run_init_db():
            try:
                database.init_db()
            except Exception as exc:  # pragma: no cover - asserted below
                init_errors.append(exc)
            finally:
                init_completed.set()

        with patch.object(database, "DB_PATH", db_file):
            lock_thread = threading.Thread(target=hold_exclusive_lock)
            init_thread = threading.Thread(target=run_init_db)

            lock_thread.start()
            assert lock_acquired.wait(timeout=5)

            init_thread.start()
            time.sleep(0.2)
            assert init_completed.is_set() is False

            release_lock.set()

            init_thread.join(timeout=5)
            lock_thread.join(timeout=5)

        assert init_errors == []
        assert init_completed.is_set() is True

        with sqlite3.connect(db_file) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'searches'"
            ).fetchone()

        assert row is not None


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
# /api/lookup-progress
# ---------------------------------------------------------------------------

class TestLookupProgress:
    def test_lookup_progress_requires_lookup_id(self, client):
        resp = client.get("/api/lookup-progress")
        assert resp.status_code == 400

    def test_lookup_progress_returns_page_percentage(self, client):
        app_module.update_lookup_progress(
            "lookup-123",
            username="testuser",
            artist="Wanda",
            track="Bologna",
            stage="recent-track-fallback",
            status="Still scanning older pages",
            detail="Checked 12 of 48 recent-track pages while walking backward through your history.",
            pages_checked=12,
            pages_total=48,
        )

        resp = client.get("/api/lookup-progress?lookup_id=lookup-123")
        data = resp.get_json()

        assert resp.status_code == 200
        assert data["found"] is True
        assert data["pages_checked"] == 12
        assert data["pages_total"] == 48
        assert data["progress_percent"] == 25
        assert data["status"] == "Still scanning older pages"


# ---------------------------------------------------------------------------
# /api/first-listen
# ---------------------------------------------------------------------------

class TestFirstListen:
    def test_missing_params_returns_400(self, client):
        resp = client.get("/api/first-listen")
        assert resp.status_code == 400

        resp = client.get("/api/first-listen?track=Hello&username=testuser")
        assert resp.status_code == 400

        resp = client.get("/api/first-listen?artist=Adele&username=testuser")
        assert resp.status_code == 400

        resp = client.get("/api/first-listen?track=Hello&artist=Adele")
        assert resp.status_code == 400

    @patch.object(app_module, "lastfm_get")
    def test_never_listened_track(self, mock_get, client):
        mock_get.return_value = _track_info_response(userplaycount=0)
        data, status = _await_first_listen(client, "track=Unknown&artist=Nobody&username=testuser")
        assert data["found"] is False
        assert "never" in data["message"].lower()

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_track_found_with_exact_date(self, mock_get, mock_public_date, client):
        """Full happy path: the public track page exposes the exact date."""
        mock_get.return_value = _track_info_response(
            userplaycount=42,
            track="TestTrack",
            artist="TestArtist",
            album="TestAlbum",
            image_url="https://img.example/cover.jpg",
        )
        mock_public_date.return_value = "02 Jan 2007, 20:30"
        data, status = _await_first_listen(client, "track=TestTrack&artist=TestArtist&username=testuser")

        assert data["found"] is True
        assert data["track"] == "TestTrack"
        assert data["artist"] == "TestArtist"
        assert data["album"] == "TestAlbum"
        assert data["total_scrobbles"] == 42
        assert data["date"] == "02 Jan 2007, 20:30"
        assert data["timestamp"] == "1167766200"
        assert data["date_unavailable"] is False
        assert data["image"] == "https://img.example/cover.jpg"
        assert data["cached"] is False
        assert isinstance(data["elapsed_ms"], int)
        assert data["elapsed_ms"] >= 0

    @patch.object(app_module, "lastfm_get")
    def test_no_chart_history_returns_error(self, mock_get, client):
        """Track lookup errors from Last.fm still surface as backend errors."""
        mock_get.side_effect = app_module.requests.HTTPError("503 Server Error")
        data, status = _await_first_listen(client, "track=Song&artist=Band&username=testuser")
        assert "error" in data

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_unavailable_when_public_track_page_has_no_timestamp(self, mock_get, mock_public_date, client):
        """If the public track page has no date, do not invent one."""
        mock_get.return_value = _track_info_response(userplaycount=10, track="X", artist="Y")
        mock_public_date.return_value = None
        data, status = _await_first_listen(client, "track=X&artist=Y&username=testuser")

        assert data["found"] is True
        assert data["date"] == ""
        assert data["timestamp"] == ""
        assert data["date_unavailable"] is True
        assert "exact first-listen timestamp" in data["date_unavailable_reason"].lower()

    @patch.object(app_module, "recent_tracks_first_listen")
    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_partial_lookup_is_saved_before_slow_resolution_finishes(
        self, mock_get, mock_public_date, mock_recent_first_listen, client
    ):
        mock_get.return_value = _track_info_response(
            userplaycount=73,
            track="Bologna",
            artist="Wanda",
            album="Amore",
            image_url="https://img.example/bologna.jpg",
        )
        mock_public_date.return_value = None
        mock_recent_first_listen.side_effect = RuntimeError("scan aborted")

        # The async lookup catches the RuntimeError in the background thread,
        # so we just wait for the lookup to finish (with an error result).
        data, status = _await_first_listen(
            client, "track=Bologna&artist=Wanda&username=testuser"
        )

        cached = database.get_cached("testuser", "Bologna", "Wanda")
        assert cached is not None
        assert cached["album"] == "Amore"
        assert cached["total_scrobbles"] == 73
        assert cached["image"] == "https://img.example/bologna.jpg"
        assert cached["first_listen_date"] == ""
        assert cached["first_listen_timestamp"] == ""

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_private_or_sparse_history_returns_unavailable_date(self, mock_get, mock_public_date, client):
        """If Last.fm reports a playcount but the public track page has no exact date, return unavailable."""
        mock_get.return_value = _track_info_response(userplaycount=1, track="Rare Song", artist="Rare Artist")
        mock_public_date.return_value = None
        data, status = _await_first_listen(client, "track=Rare+Song&artist=Rare+Artist&username=testuser")

        assert data["found"] is True
        assert data["date"] == ""
        assert data["timestamp"] == ""
        assert data["date_unavailable"] is True
        assert "exact first-listen timestamp" in data["date_unavailable_reason"].lower()

    @patch.object(app_module, "lastfm_get")
    def test_track_getinfo_http_error_returns_502(self, mock_get, client):
        import requests as req
        mock_get.side_effect = req.HTTPError("503 Server Error")
        data, status = _await_first_listen(client, "track=A&artist=B&username=testuser")
        assert "error" in data

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_single_page_history(self, mock_get, mock_public_date, client):
        """A single public track page date is returned correctly."""
        mock_get.return_value = _track_info_response(userplaycount=1, track="Only", artist="One")
        mock_public_date.return_value = "03 Jan 2007, 10:00"
        data, status = _await_first_listen(client, "track=Only&artist=One&username=testuser")

        assert data["found"] is True
        assert data["date"] == "03 Jan 2007, 10:00"
        assert data["timestamp"] == "1167814800"
        assert data["total_scrobbles"] == 1

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_case_insensitive_matching(self, mock_get, mock_public_date, client):
        """Canonical names from track.getInfo are used for the public page lookup."""
        mock_get.return_value = _track_info_response(userplaycount=3, track="My Song", artist="The Band")
        mock_public_date.return_value = "15 Jan 2007, 12:00"
        data, status = _await_first_listen(client, "track=My+Song&artist=The+Band&username=testuser")
        assert data["found"] is True
        assert data["timestamp"] == "1168858800"

    @patch.object(app_module, "public_library_first_listen_date", return_value=None)
    @patch.object(app_module, "lastfm_get")
    def test_old_track_falls_back_to_recent_track_history(self, mock_get, mock_public_date, client):
        """Older tracks should still resolve by scanning recent-track pages from oldest to newest."""

        def fake_lastfm(method, **params):
            if method == "track.getInfo":
                return _track_info_response(
                    userplaycount=111,
                    track="My First Trumpet",
                    artist="Autonarkose",
                    album="My First Trumpet",
                )
            if method == "user.getRecentTracks":
                page = int(params["page"])
                if page == 1:
                    return _recent_tracks_response(
                        [("Recent Song", "Elsewhere", "01 Jan 2025, 18:00", "1735754400")],
                        page=1,
                        total_pages=3,
                    )
                if page == 3:
                    return _recent_tracks_response(
                        [
                            ("My First Trumpet", "Autonarkose", "05 Mar 2008, 21:17", "1204751820"),
                            ("Earlier Other Track", "Someone", "04 Mar 2008, 10:00", "1204624800"),
                        ],
                        page=3,
                        total_pages=3,
                    )
                return _recent_tracks_response(
                    [("Middle Song", "Elsewhere", "01 Jan 2016, 12:00", "1451649600")],
                    page=2,
                    total_pages=3,
                )
            raise AssertionError(f"Unexpected method: {method}")

        mock_get.side_effect = fake_lastfm

        data, status = _await_first_listen(
            client, "track=My+First+Trumpet&artist=Autonarkose&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "05 Mar 2008, 21:17"
        assert data["timestamp"] == "1204751820"
        assert data["date_unavailable"] is False

    @patch.object(app_module, "public_library_first_listen_date", return_value=None)
    @patch.object(app_module, "lastfm_get")
    def test_missing_userplaycount_uses_recent_history_summary(self, mock_get, mock_public_date, client):
        """If Last.fm omits userplaycount, derive both first listen and playcount from recent-track history."""

        def fake_lastfm(method, **params):
            if method == "track.getInfo":
                return _track_info_response(
                    userplaycount=None,
                    track="Missing Count Song",
                    artist="Hidden Artist",
                )
            if method == "user.getRecentTracks":
                page = int(params["page"])
                if page == 1:
                    return _recent_tracks_response(
                        [
                            ("Missing Count Song", "Hidden Artist", "02 Jan 2012, 08:00", "1325491200"),
                            ("Something Else", "Someone", "03 Jan 2012, 08:00", "1325577600"),
                        ],
                        page=1,
                        total_pages=2,
                    )
                return _recent_tracks_response(
                    [
                        ("Missing Count Song", "Hidden Artist", "01 Jan 2012, 08:00", "1325404800"),
                        ("Older Other Song", "Someone", "31 Dec 2011, 08:00", "1325318400"),
                    ],
                    page=2,
                    total_pages=2,
                )
            raise AssertionError(f"Unexpected method: {method}")

        mock_get.side_effect = fake_lastfm

        data, status = _await_first_listen(
            client, "track=Missing+Count+Song&artist=Hidden+Artist&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "01 Jan 2012, 08:00"
        assert data["timestamp"] == "1325404800"
        assert data["total_scrobbles"] == 2


# ---------------------------------------------------------------------------
# Database caching
# ---------------------------------------------------------------------------

class TestDatabaseCaching:
    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_result_is_cached_after_first_query(self, mock_get, mock_public_date, client):
        """After a successful lookup the result must be stored in the DB."""
        mock_get.return_value = _track_info_response(userplaycount=5, track="Cached Song", artist="Artist")
        mock_public_date.return_value = "10 Jan 2007, 09:00"
        data, status = _await_first_listen(client, "track=Cached+Song&artist=Artist&username=testuser")
        assert data["found"] is True
        assert data["cached"] is False
        assert isinstance(data["elapsed_ms"], int)
        assert data["elapsed_ms"] >= 0

        # Verify it was stored
        stored = database.get_cached("testuser", "Cached Song", "Artist")
        assert stored is not None
        assert stored["track"] == "Cached Song"
        assert stored["first_listen_date"] == "10 Jan 2007, 09:00"

    @patch.object(app_module, "public_library_first_listen_date", return_value=None)
    @patch.object(app_module, "lastfm_get")
    def test_cached_unavailable_date_triggers_retry(self, mock_get, mock_public_date, client):
        """Cached rows without a date should NOT be served from cache — the app retries the lookup."""
        database.save_result(
            "testuser",
            "Sparse Song",
            "Sparse Artist",
            "",
            "",
            "",
            1,
            "",
        )

        mock_get.return_value = _track_info_response(userplaycount=1, track="Sparse Song", artist="Sparse Artist")
        mock_public_date.return_value = None

        data, status = _await_first_listen(client, "track=Sparse+Song&artist=Sparse+Artist&username=testuser")

        assert data["found"] is True
        # Should NOT come from cache — a fresh API call should be made
        assert data["cached"] is False
        assert isinstance(data["elapsed_ms"], int)
        assert data["elapsed_ms"] >= 0
        assert data["date_unavailable"] is True

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
        database.save_artist_first_listen(
            "testuser", "Bruno Mars",
            "Just The Way You Are", "01 Jan 2011, 10:00", "1293872400",
        )

        resp = client.get("/api/first-listen?track=Lazy+Song&artist=Bruno+Mars&username=testuser")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        assert data["track"] == "Lazy Song"
        assert data["date"] == "01 May 2011, 14:00"
        assert data["total_scrobbles"] == 88
        assert isinstance(data["elapsed_ms"], int)
        assert data["elapsed_ms"] >= 0
        # The Last.fm API must not have been called at all
        mock_get.assert_not_called()

    @patch.object(app_module, "lastfm_get")
    def test_cache_hit_updates_queried_at(self, mock_get, client):
        """A second call for the same track (served from cache) must refresh
        queried_at so the track rises to the top of the lookup history."""
        database.save_result(
            "testuser", "Old Hit", "Classic Band",
            "Greatest Hits",
            "01 Jan 2005, 10:00", "1104573600",
            50, "",
        )
        database.save_artist_first_listen(
            "testuser", "Classic Band",
            "Old Hit", "01 Jan 2005, 10:00", "1104573600",
        )
        queried_at_before = database.get_cached("testuser", "Old Hit", "Classic Band")["queried_at"]

        # Small sleep so the new timestamp is strictly greater
        time.sleep(0.01)

        resp = client.get("/api/first-listen?track=Old+Hit&artist=Classic+Band&username=testuser")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        mock_get.assert_not_called()

        queried_at_after = database.get_cached("testuser", "Old Hit", "Classic Band")["queried_at"]
        assert queried_at_after > queried_at_before, (
            "queried_at must be updated on a cache hit so the track moves to the top of history"
        )


        """Cache hit should work regardless of the casing used in the query."""
        database.save_result(
            "testuser", "Bohemian Rhapsody", "Queen",
            "A Night at the Opera",
            "15 Mar 2005, 20:00", "1110924000",
            1000, "",
        )
        database.save_artist_first_listen(
            "testuser", "Queen",
            "Bohemian Rhapsody", "15 Mar 2005, 20:00", "1110924000",
        )

        resp = client.get("/api/first-listen?track=bohemian+rhapsody&artist=queen&username=testuser")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        assert isinstance(data["elapsed_ms"], int)
        assert data["elapsed_ms"] >= 0
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# /api/history
# ---------------------------------------------------------------------------

class TestHistory:
    def test_empty_history(self, client):
        resp = client.get("/api/history?username=testuser")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_history_returns_saved_results(self, client):
        database.save_result(
            "testuser", "Track One", "Artist A", "Album A",
            "01 Jan 2010, 12:00", "1262347200", 10, "",
        )
        database.save_result(
            "testuser", "Track Two", "Artist B", "Album B",
            "15 Jun 2015, 18:30", "1434389400", 25, "",
        )

        resp = client.get("/api/history?username=testuser")
        data = resp.get_json()

        assert len(data) == 2
        tracks = {r["track"] for r in data}
        assert tracks == {"Track One", "Track Two"}

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

    def test_history_order_is_stable_with_same_timestamp(self, client, isolated_db):
        """When two rows share an identical queried_at the higher id (newer insert)
        must always come first — verified by querying repeatedly."""
        fixed_ts = "2024-06-01T12:00:00+00:00"
        with sqlite3.connect(isolated_db) as conn:
            conn.execute(
                "INSERT INTO searches (username, track, artist, album, first_listen_date, "
                "first_listen_timestamp, total_scrobbles, image, queried_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("testuser", "Track Alpha", "Artist", "", "01 Jan 2010, 00:00", "1262304000", 1, "", fixed_ts),
            )
            conn.execute(
                "INSERT INTO searches (username, track, artist, album, first_listen_date, "
                "first_listen_timestamp, total_scrobbles, image, queried_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("testuser", "Track Beta", "Artist", "", "01 Jan 2011, 00:00", "1293840000", 1, "", fixed_ts),
            )
            conn.commit()

        for _ in range(5):
            resp = client.get("/api/history?username=testuser")
            data = resp.get_json()
            assert len(data) == 2
            assert data[0]["track"] == "Track Beta", "Higher id must always sort first for equal timestamps"
            assert data[1]["track"] == "Track Alpha"


# ---------------------------------------------------------------------------
# /api/listening-history
# ---------------------------------------------------------------------------

class TestListeningHistory:
    """Tests for the listening-history endpoint."""

    def test_missing_params_returns_400(self, client):
        resp = client.get("/api/listening-history?username=u&track=t")
        assert resp.status_code == 400

    def test_empty_chart_list_returns_empty(self, client):
        with patch("app.lastfm_get") as mock_get:
            mock_get.return_value = {"weeklychartlist": {"chart": []}}
            resp = client.get("/api/listening-history?username=u&track=Song&artist=Band")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_returns_monthly_play_counts(self, client):
        import calendar
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        # Build 4 chart weeks within the current month
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        weeks = []
        for i in range(4):
            from_ts = int(month_start.timestamp()) + i * 7 * 86400
            to_ts = from_ts + 7 * 86400
            weeks.append((from_ts, to_ts))

        chart_list = _weekly_chart_list(weeks)
        weekly_chart_with_track = _weekly_track_chart([("MySong", "MyArtist", 3)])
        weekly_chart_empty = _weekly_track_chart([("OtherSong", "OtherArtist", 5)])

        def fake_get(method, **kwargs):
            if method == "user.getWeeklyChartList":
                return chart_list
            if method == "user.getWeeklyTrackChart":
                # Return matching track for first two weeks, empty for the rest
                from_val = int(kwargs.get("from", 0))
                if from_val in (weeks[0][0], weeks[1][0]):
                    return weekly_chart_with_track
                return weekly_chart_empty
            return {}

        with patch("app.lastfm_get", side_effect=fake_get):
            resp = client.get("/api/listening-history?username=u&track=MySong&artist=MyArtist&months=2")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) >= 1
            month_key = month_start.strftime("%Y-%m")
            entry = next((d for d in data if d["month"] == month_key), None)
            assert entry is not None
            assert entry["plays"] == 6  # 3 plays × 2 weeks
            assert "label" in entry

    def test_chart_list_api_failure_returns_502(self, client):
        with patch("app.lastfm_get", side_effect=Exception("API down")):
            resp = client.get("/api/listening-history?username=u&track=t&artist=a")
            assert resp.status_code == 502

    def test_weekly_chart_failure_still_returns_data(self, client):
        """If individual weekly chart calls fail, the month should still appear with 0 plays."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        from_ts = int(month_start.timestamp())
        to_ts = from_ts + 7 * 86400

        call_count = [0]

        def fake_get(method, **kwargs):
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list([(from_ts, to_ts)])
            call_count[0] += 1
            raise Exception("chart fetch failed")

        with patch("app.lastfm_get", side_effect=fake_get):
            resp = client.get("/api/listening-history?username=u&track=t&artist=a&months=2")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) >= 1
            assert data[0]["plays"] == 0

    def test_cached_response_avoids_api_calls(self, client):
        """Second request for the same track should be served from cache."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        from_ts = int(month_start.timestamp())
        to_ts = from_ts + 7 * 86400

        api_calls = [0]

        def fake_get(method, **kwargs):
            api_calls[0] += 1
            if method == "user.getWeeklyChartList":
                return _weekly_chart_list([(from_ts, to_ts)])
            return _weekly_track_chart([("CachedSong", "CachedArtist", 5)])

        with patch("app.lastfm_get", side_effect=fake_get):
            resp1 = client.get("/api/listening-history?username=cacheuser&track=CachedSong&artist=CachedArtist&months=2")
            assert resp1.status_code == 200
            calls_after_first = api_calls[0]

            resp2 = client.get("/api/listening-history?username=cacheuser&track=CachedSong&artist=CachedArtist&months=2")
            assert resp2.status_code == 200
            assert api_calls[0] == calls_after_first, "Second request should not trigger new API calls"
            assert resp1.get_json() == resp2.get_json()


# ---------------------------------------------------------------------------
# database.artist_first_listens
# ---------------------------------------------------------------------------

class TestArtistFirstListenDatabase:
    def test_get_returns_none_when_not_stored(self):
        result = database.get_artist_first_listen("testuser", "Unknown Artist")
        assert result is None

    def test_save_and_retrieve(self):
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Creep", "01 Mar 1993, 12:00", "731160000",
        )
        row = database.get_artist_first_listen("testuser", "Radiohead")
        assert row is not None
        assert row["artist"] == "Radiohead"
        assert row["first_listen_track"] == "Creep"
        assert row["first_listen_date"] == "01 Mar 1993, 12:00"
        assert row["first_listen_timestamp"] == "731160000"

    def test_save_is_case_insensitive(self):
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Creep", "01 Mar 1993, 12:00", "731160000",
        )
        row = database.get_artist_first_listen("TESTUSER", "radiohead")
        assert row is not None
        assert row["first_listen_track"] == "Creep"

    def test_save_updates_existing_record(self):
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Creep", "01 Mar 1993, 12:00", "731160000",
        )
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Fake Plastic Trees", "01 Mar 1993, 10:00", "731153200",
        )
        row = database.get_artist_first_listen("testuser", "Radiohead")
        assert row["first_listen_track"] == "Fake Plastic Trees"

    def test_separate_users_isolated(self):
        database.save_artist_first_listen(
            "alice", "Radiohead", "Creep", "01 Mar 1993, 12:00", "731160000",
        )
        database.save_artist_first_listen(
            "bob", "Radiohead", "High and Dry", "10 Apr 1993, 12:00", "734356800",
        )
        alice_row = database.get_artist_first_listen("alice", "Radiohead")
        bob_row = database.get_artist_first_listen("bob", "Radiohead")
        assert alice_row["first_listen_track"] == "Creep"
        assert bob_row["first_listen_track"] == "High and Dry"


# ---------------------------------------------------------------------------
# /api/artist-first-listen endpoint
# ---------------------------------------------------------------------------

class TestArtistFirstListenEndpoint:
    def test_missing_params_returns_400(self, client):
        resp = client.get("/api/artist-first-listen")
        assert resp.status_code == 400

        resp = client.get("/api/artist-first-listen?username=testuser")
        assert resp.status_code == 400

        resp = client.get("/api/artist-first-listen?artist=Radiohead")
        assert resp.status_code == 400

    @patch.object(app_module, "_find_and_store_artist_first_listen")
    def test_returns_cached_data(self, mock_find, client):
        mock_find.return_value = {
            "first_listen_date": "01 Mar 1993, 12:00",
            "first_listen_timestamp": "731160000",
            "first_listen_track": "Creep",
        }
        resp = client.get("/api/artist-first-listen?username=testuser&artist=Radiohead")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["artist"] == "Radiohead"
        assert data["username"] == "testuser"
        assert data["first_listen_date"] == "01 Mar 1993, 12:00"
        assert data["first_listen_track"] == "Creep"

    def test_stored_result_served_directly(self, client):
        database.save_artist_first_listen(
            "testuser", "Weezer",
            "Buddy Holly", "10 Jan 2005, 15:00", "1105368000",
        )
        with patch.object(app_module, "lastfm_get") as mock_get, \
             patch.object(app_module, "public_library_artist_first_listen") as mock_lib:
            resp = client.get("/api/artist-first-listen?username=testuser&artist=Weezer")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["first_listen_date"] == "10 Jan 2005, 15:00"
        assert data["first_listen_track"] == "Buddy Holly"
        # Library scraping should be skipped since we already have the data
        mock_lib.assert_not_called()


# ---------------------------------------------------------------------------
# artist_first_listen is fetched asynchronously (not included in first-listen result)
# ---------------------------------------------------------------------------

class TestFirstListenExcludesArtistDiscovery:
    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_result_omits_artist_first_listen_fields(
        self, mock_get, mock_public_date, client
    ):
        mock_get.return_value = _track_info_response(
            userplaycount=10, track="Song", artist="Band"
        )
        mock_public_date.return_value = "05 Jun 2010, 14:00"

        data, status = _await_first_listen(
            client, "track=Song&artist=Band&username=testuser"
        )

        assert data["found"] is True
        assert "artist_first_listen_date" not in data
        assert "artist_first_listen_timestamp" not in data
        assert "artist_first_listen_track" not in data

    @patch.object(app_module, "lastfm_get")
    def test_cache_hit_omits_artist_first_listen_fields(self, mock_get, client):
        database.save_result(
            "testuser", "Cached Song", "Cached Artist",
            "Album", "01 Feb 2015, 10:00", "1422784800", 5, "",
        )
        database.save_artist_first_listen(
            "testuser", "Cached Artist",
            "First Song Ever", "01 Jan 2015, 08:00", "1420099200",
        )

        resp = client.get("/api/first-listen?track=Cached+Song&artist=Cached+Artist&username=testuser")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        assert "artist_first_listen_date" not in data
        assert "artist_first_listen_timestamp" not in data
        assert "artist_first_listen_track" not in data
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# artist_first_listen is automatically updated when track lookup finds earlier date
# ---------------------------------------------------------------------------

class TestArtistFirstListenAutoUpdate:
    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_artist_first_listen_updated_when_track_is_earlier(
        self, mock_get, mock_public_date, client
    ):
        """When a track's first listen is earlier than the artist's cached first listen,
        the artist first listen should be automatically updated."""
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Karma Police", "10 Jun 2010, 14:00", "1276178400",
        )

        mock_get.return_value = _track_info_response(
            userplaycount=5, track="Creep", artist="Radiohead"
        )
        mock_public_date.return_value = "01 Mar 1993, 12:00"

        data, status = _await_first_listen(
            client, "track=Creep&artist=Radiohead&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "01 Mar 1993, 12:00"

        artist_cached = database.get_artist_first_listen("testuser", "Radiohead")
        assert artist_cached is not None
        assert artist_cached["first_listen_date"] == "01 Mar 1993, 12:00"
        assert artist_cached["first_listen_track"] == "Creep"

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_artist_first_listen_not_updated_when_track_is_later(
        self, mock_get, mock_public_date, client
    ):
        """When a track's first listen is later than the artist's cached first listen,
        the artist first listen should not be updated."""
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Creep", "01 Mar 1993, 12:00", "731160000",
        )

        mock_get.return_value = _track_info_response(
            userplaycount=3, track="Karma Police", artist="Radiohead"
        )
        mock_public_date.return_value = "10 Jun 2010, 14:00"

        data, status = _await_first_listen(
            client, "track=Karma+Police&artist=Radiohead&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "10 Jun 2010, 14:00"

        artist_cached = database.get_artist_first_listen("testuser", "Radiohead")
        assert artist_cached is not None
        assert artist_cached["first_listen_date"] == "01 Mar 1993, 12:00"
        assert artist_cached["first_listen_track"] == "Creep"

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_artist_first_listen_not_created_from_track_lookup(
        self, mock_get, mock_public_date, client
    ):
        """When no artist first listen exists, the track lookup must NOT create one.

        Creating an entry here would pre-seed the cache and prevent the dedicated
        /api/artist-first-listen endpoint from running the full artist library scrape.
        """
        mock_get.return_value = _track_info_response(
            userplaycount=5, track="Fake Plastic Trees", artist="Radiohead"
        )
        mock_public_date.return_value = "15 May 1995, 09:30"

        data, status = _await_first_listen(
            client, "track=Fake+Plastic+Trees&artist=Radiohead&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "15 May 1995, 09:30"

        artist_cached = database.get_artist_first_listen("testuser", "Radiohead")
        assert artist_cached is None

    def test_artist_first_listen_updated_from_cache_hit(self, client):
        """When a cached track result is earlier than artist first listen, update it."""
        database.save_result(
            "testuser", "No Surprises", "Radiohead",
            "OK Computer", "20 Aug 1997, 16:00", "872092800", 12, "",
        )

        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Karma Police", "10 Jun 2010, 14:00", "1276178400",
        )

        resp = client.get("/api/first-listen?track=No+Surprises&artist=Radiohead&username=testuser")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        assert data["date"] == "20 Aug 1997, 16:00"

        artist_cached = database.get_artist_first_listen("testuser", "Radiohead")
        assert artist_cached is not None
        assert artist_cached["first_listen_date"] == "20 Aug 1997, 16:00"
        assert artist_cached["first_listen_track"] == "No Surprises"

    @patch("app.requests.get")
    @patch.object(app_module, "_oldest_scrobble_on_track_page")
    @patch.object(app_module, "lastfm_get")
    def test_artist_first_listen_checks_all_tracks(
        self, mock_lastfm_get, mock_oldest, mock_requests_get
    ):
        """Ensure the lookup examines all tracks, not just a subset."""
        mock_lastfm_get.return_value = {"artist": {"stats": {"userplaycount": "25"}}}

        tracks = [f"Track{i}" for i in range(1, 13)]
        artist_html = "".join(
            f'<a href="/music/Enno+Bunger/_/{t}">{t}</a>' for t in tracks
        )

        class FakeResponse:
            def __init__(self, text):
                self.status_code = 200
                self.text = text
                self.url = "https://last.fm/fake"

            def raise_for_status(self):
                return None

        mock_requests_get.return_value = FakeResponse(artist_html)

        earliest_date = ("01 Jan 2009, 10:00", "1230794400")
        later_date = ("01 Jan 2014, 10:00", "1388570400")

        calls: list[str] = []

        def fake_oldest(username, artist, track_name_encoded, headers):
            calls.append(track_name_encoded)
            track_name = unquote(track_name_encoded.replace("+", " "))
            if track_name == "Track1":
                return earliest_date
            return later_date

        mock_oldest.side_effect = fake_oldest

        result = app_module._find_and_store_artist_first_listen("jet1985", "Enno Bunger")

        called_tracks = {unquote(c.replace("+", " ")) for c in calls}
        assert "Track1" in called_tracks
        assert len(calls) == 12
        assert result["first_listen_track"] == "Track1"
        assert result["first_listen_date"] == earliest_date[0]
