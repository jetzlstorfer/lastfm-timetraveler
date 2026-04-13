"""Tests for automatic artist first listen update fix."""

import time
from unittest.mock import patch

import pytest

import database
import app as app_module
from app import app


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
                    {"#text": image_url, "size": "extralarge"},
                ],
            },
        }
    }
    if userplaycount is not None:
        response["track"]["userplaycount"] = str(userplaycount)
    return response


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
        # First, set up an existing artist first listen that is later
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Karma Police", "10 Jun 2010, 14:00", "1276178400",
        )

        # Now look up a track that has an earlier first listen
        mock_get.return_value = _track_info_response(
            userplaycount=5, track="Creep", artist="Radiohead"
        )
        mock_public_date.return_value = "01 Mar 1993, 12:00"

        data, status = _await_first_listen(
            client, "track=Creep&artist=Radiohead&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "01 Mar 1993, 12:00"

        # Verify artist first listen was updated
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
        # First, set up an existing artist first listen that is earlier
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Creep", "01 Mar 1993, 12:00", "731160000",
        )

        # Now look up a track that has a later first listen
        mock_get.return_value = _track_info_response(
            userplaycount=3, track="Karma Police", artist="Radiohead"
        )
        mock_public_date.return_value = "10 Jun 2010, 14:00"

        data, status = _await_first_listen(
            client, "track=Karma+Police&artist=Radiohead&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "10 Jun 2010, 14:00"

        # Verify artist first listen was NOT updated
        artist_cached = database.get_artist_first_listen("testuser", "Radiohead")
        assert artist_cached is not None
        assert artist_cached["first_listen_date"] == "01 Mar 1993, 12:00"
        assert artist_cached["first_listen_track"] == "Creep"

    @patch.object(app_module, "public_library_first_listen_date")
    @patch.object(app_module, "lastfm_get")
    def test_artist_first_listen_created_when_no_existing_data(
        self, mock_get, mock_public_date, client
    ):
        """When no artist first listen exists, it should be created from the track lookup."""
        mock_get.return_value = _track_info_response(
            userplaycount=5, track="Fake Plastic Trees", artist="Radiohead"
        )
        mock_public_date.return_value = "15 May 1995, 09:30"

        data, status = _await_first_listen(
            client, "track=Fake+Plastic+Trees&artist=Radiohead&username=testuser"
        )

        assert data["found"] is True
        assert data["date"] == "15 May 1995, 09:30"

        # Verify artist first listen was created
        artist_cached = database.get_artist_first_listen("testuser", "Radiohead")
        assert artist_cached is not None
        assert artist_cached["first_listen_date"] == "15 May 1995, 09:30"
        assert artist_cached["first_listen_track"] == "Fake Plastic Trees"

    def test_artist_first_listen_updated_from_cache_hit(self, client):
        """When a cached track result is earlier than artist first listen, update it."""
        # Save a cached track result
        database.save_result(
            "testuser", "No Surprises", "Radiohead",
            "OK Computer", "20 Aug 1997, 16:00", "872092800", 12, "",
        )

        # Save an artist first listen that is later
        database.save_artist_first_listen(
            "testuser", "Radiohead",
            "Karma Police", "10 Jun 2010, 14:00", "1276178400",
        )

        # Request the cached track
        resp = client.get("/api/first-listen?track=No+Surprises&artist=Radiohead&username=testuser")
        data = resp.get_json()

        assert data["found"] is True
        assert data["cached"] is True
        assert data["date"] == "20 Aug 1997, 16:00"

        # Verify artist first listen was updated
        artist_cached = database.get_artist_first_listen("testuser", "Radiohead")
        assert artist_cached is not None
        assert artist_cached["first_listen_date"] == "20 Aug 1997, 16:00"
        assert artist_cached["first_listen_track"] == "No Surprises"
