"""Tests for the Last.fm Time Traveler app."""

import json
import os
import sqlite3
import tempfile
import threading
import time
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
        with app_module.LOOKUP_PROGRESS_LOCK:
            app_module.LOOKUP_PROGRESS.clear()
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
        resp = client.get("/api/first-listen?track=Unknown&artist=Nobody&username=testuser")
        data = resp.get_json()
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
        resp = client.get("/api/first-listen?track=TestTrack&artist=TestArtist&username=testuser")
        data = resp.get_json()

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
        resp = client.get("/api/first-listen?track=Song&artist=Band&username=testuser")
        assert resp.status_code == 502

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_unavailable_when_public_track_page_has_no_timestamp(self, mock_get, mock_public_date, client):
        """If the public track page has no date, do not invent one."""
        mock_get.return_value = _track_info_response(userplaycount=10, track="X", artist="Y")
        mock_public_date.return_value = None
        resp = client.get("/api/first-listen?track=X&artist=Y&username=testuser")
        data = resp.get_json()

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

        with pytest.raises(RuntimeError, match="scan aborted"):
            client.get(
                "/api/first-listen?track=Bologna&artist=Wanda&username=testuser"
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
        resp = client.get("/api/first-listen?track=Rare+Song&artist=Rare+Artist&username=testuser")
        data = resp.get_json()

        assert data["found"] is True
        assert data["date"] == ""
        assert data["timestamp"] == ""
        assert data["date_unavailable"] is True
        assert "exact first-listen timestamp" in data["date_unavailable_reason"].lower()

    @patch.object(app_module, "lastfm_get")
    def test_track_getinfo_http_error_returns_502(self, mock_get, client):
        import requests as req
        mock_get.side_effect = req.HTTPError("503 Server Error")
        resp = client.get("/api/first-listen?track=A&artist=B&username=testuser")
        assert resp.status_code == 502

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_single_page_history(self, mock_get, mock_public_date, client):
        """A single public track page date is returned correctly."""
        mock_get.return_value = _track_info_response(userplaycount=1, track="Only", artist="One")
        mock_public_date.return_value = "03 Jan 2007, 10:00"
        resp = client.get("/api/first-listen?track=Only&artist=One&username=testuser")
        data = resp.get_json()

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
        resp = client.get("/api/first-listen?track=My+Song&artist=The+Band&username=testuser")
        data = resp.get_json()
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

        resp = client.get(
            "/api/first-listen?track=My+First+Trumpet&artist=Autonarkose&username=testuser"
        )
        data = resp.get_json()

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

        resp = client.get(
            "/api/first-listen?track=Missing+Count+Song&artist=Hidden+Artist&username=testuser"
        )
        data = resp.get_json()

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
        resp = client.get("/api/first-listen?track=Cached+Song&artist=Artist&username=testuser")
        data = resp.get_json()
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

        resp = client.get("/api/first-listen?track=Sparse+Song&artist=Sparse+Artist&username=testuser")
        data = resp.get_json()

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

