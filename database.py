"""SQLite persistence layer for Last.fm Time Traveler.

Each resolved first-listen result is stored so that repeated queries for the
same (username, track, artist) triple are served instantly from the local cache
instead of repeating the expensive binary-search over Last.fm's weekly charts.

The database path defaults to ``timetraveler.db`` in the working directory and
can be overridden with the ``DB_PATH`` environment variable (set ``DB_PATH=:memory:``
in tests for an isolated in-memory database).
"""

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "timetraveler.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the searches table if it does not exist yet."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                username                TEXT    NOT NULL,
                track                   TEXT    NOT NULL,
                artist                  TEXT    NOT NULL,
                album                   TEXT,
                first_listen_date       TEXT,
                first_listen_timestamp  TEXT,
                total_scrobbles         INTEGER,
                image                   TEXT,
                queried_at              TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_searches_user_track_artist
            ON searches (LOWER(username), LOWER(track), LOWER(artist))
            """
        )
        conn.commit()


def get_cached(username: str, track: str, artist: str) -> dict | None:
    """Return the stored result for *(username, track, artist)*, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM searches
            WHERE LOWER(username) = LOWER(?)
              AND LOWER(track)    = LOWER(?)
              AND LOWER(artist)   = LOWER(?)
            """,
            (username, track, artist),
        ).fetchone()
    return dict(row) if row else None


def save_result(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> None:
    """Insert or update a first-listen result in the database."""
    queried_at = datetime.now(timezone.utc).isoformat()
    existing = get_cached(username, track, artist)
    with _connect() as conn:
        if existing:
            conn.execute(
                """
                UPDATE searches
                SET album                  = ?,
                    first_listen_date      = ?,
                    first_listen_timestamp = ?,
                    total_scrobbles        = ?,
                    image                  = ?,
                    queried_at             = ?
                WHERE id = ?
                """,
                (
                    album,
                    first_listen_date,
                    first_listen_timestamp,
                    total_scrobbles,
                    image,
                    queried_at,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO searches
                    (username, track, artist, album, first_listen_date,
                     first_listen_timestamp, total_scrobbles, image, queried_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    track,
                    artist,
                    album,
                    first_listen_date,
                    first_listen_timestamp,
                    total_scrobbles,
                    image,
                    queried_at,
                ),
            )
        conn.commit()


def get_history(username: str) -> list[dict]:
    """Return all stored searches for *username*, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM searches
            WHERE LOWER(username) = LOWER(?)
            ORDER BY queried_at DESC, id DESC
            """,
            (username,),
        ).fetchall()
    return [dict(r) for r in rows]
