"""Persistence layer for Last.fm Time Traveler.

The app supports two storage backends:

- SQLite for local and test workflows.
- Azure Cosmos DB for NoSQL when Cosmos environment variables are configured.

The public API remains the same so the rest of the Flask app can treat the
cache as a simple key-value store with history queries by username.
"""

import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

try:
    from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exceptions
except ImportError:
    CosmosClient = None
    PartitionKey = None
    cosmos_exceptions = None


DEFAULT_SQLITE_DB_PATH = "timetraveler.db"
DB_PATH = os.getenv("DB_PATH", DEFAULT_SQLITE_DB_PATH)
DEFAULT_COSMOS_DATABASE_NAME = "lastfm-timetraveler"
DEFAULT_COSMOS_CONTAINER_NAME = "searches"
SQLITE_TIMEOUT_SECONDS = 30
INIT_DB_MAX_ATTEMPTS = 3
INIT_DB_RETRY_DELAY_SECONDS = 0.25
_SQLITE_INIT_LOCK = threading.Lock()
_INITIALIZED_SQLITE_DB_PATH = None
_COSMOS_INIT_LOCK = threading.Lock()
_COSMOS_SIGNATURE = None
_COSMOS_CLIENT = None
_COSMOS_DATABASE = None
_COSMOS_CONTAINER = None


def _normalize_lookup_value(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _cache_item_id(username: str, track: str, artist: str) -> str:
    return "|".join(
        [
            _normalize_lookup_value(username),
            _normalize_lookup_value(artist),
            _normalize_lookup_value(track),
        ]
    )


def _artist_first_listen_item_id(username: str, artist: str) -> str:
    return "artist_first_listen|" + "|".join(
        [
            _normalize_lookup_value(username),
            _normalize_lookup_value(artist),
        ]
    )


def _use_cosmos_backend() -> bool:
    return bool(_cosmos_connection_string() or (_cosmos_endpoint() and _cosmos_key()))


def _sqlite_db_path() -> str:
    if DB_PATH != DEFAULT_SQLITE_DB_PATH:
        return DB_PATH
    return os.getenv("DB_PATH", DEFAULT_SQLITE_DB_PATH)


def _cosmos_connection_string() -> str:
    return os.getenv("COSMOS_CONNECTION_STRING", "").strip()


def _cosmos_endpoint() -> str:
    return os.getenv("COSMOS_ENDPOINT", "").strip()


def _cosmos_key() -> str:
    return os.getenv("COSMOS_KEY", "").strip()


def _cosmos_database_name() -> str:
    return os.getenv("COSMOS_DATABASE_NAME", DEFAULT_COSMOS_DATABASE_NAME).strip() or DEFAULT_COSMOS_DATABASE_NAME


def _cosmos_container_name() -> str:
    return os.getenv("COSMOS_CONTAINER_NAME", DEFAULT_COSMOS_CONTAINER_NAME).strip() or DEFAULT_COSMOS_CONTAINER_NAME


def _sqlite_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_sqlite_db_path(), timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_TIMEOUT_SECONDS * 1000}")
    return conn


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _create_sqlite_schema(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artist_first_listens (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            username                TEXT    NOT NULL,
            artist                  TEXT    NOT NULL,
            first_listen_track      TEXT,
            first_listen_date       TEXT,
            first_listen_timestamp  TEXT,
            queried_at              TEXT    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_artist_first_listens_user_artist
        ON artist_first_listens (LOWER(username), LOWER(artist))
        """
    )
    conn.commit()


def _get_cosmos_container():
    global _COSMOS_SIGNATURE, _COSMOS_CLIENT, _COSMOS_DATABASE, _COSMOS_CONTAINER

    if CosmosClient is None or PartitionKey is None or cosmos_exceptions is None:
        raise RuntimeError(
            "azure-cosmos is not installed. Add the package and configure Cosmos DB environment variables."
        )

    connection_string = _cosmos_connection_string()
    endpoint = _cosmos_endpoint()
    key = _cosmos_key()
    database_name = _cosmos_database_name()
    container_name = _cosmos_container_name()
    signature = (connection_string, endpoint, key, database_name, container_name)

    with _COSMOS_INIT_LOCK:
        if _COSMOS_CONTAINER is not None and _COSMOS_SIGNATURE == signature:
            return _COSMOS_CONTAINER

        if connection_string:
            client = CosmosClient.from_connection_string(connection_string)
        elif endpoint and key:
            client = CosmosClient(endpoint, credential=key)
        else:
            raise RuntimeError(
                "Cosmos DB is selected but no credentials were provided. Set COSMOS_CONNECTION_STRING or COSMOS_ENDPOINT and COSMOS_KEY."
            )

        database = client.create_database_if_not_exists(id=database_name)
        container = database.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path="/username_normalized"),
        )

        _COSMOS_SIGNATURE = signature
        _COSMOS_CLIENT = client
        _COSMOS_DATABASE = database
        _COSMOS_CONTAINER = container
        return container


def _cosmos_item(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> dict:
    return {
        "id": _cache_item_id(username, track, artist),
        "type": "search",
        "username": username,
        "username_normalized": _normalize_lookup_value(username),
        "track": track,
        "track_normalized": _normalize_lookup_value(track),
        "artist": artist,
        "artist_normalized": _normalize_lookup_value(artist),
        "album": album,
        "first_listen_date": first_listen_date,
        "first_listen_timestamp": first_listen_timestamp,
        "total_scrobbles": total_scrobbles,
        "image": image,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


def _record_from_cosmos_item(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        "id": item.get("id"),
        "username": item.get("username", ""),
        "track": item.get("track", ""),
        "artist": item.get("artist", ""),
        "album": item.get("album", ""),
        "first_listen_date": item.get("first_listen_date", ""),
        "first_listen_timestamp": item.get("first_listen_timestamp", ""),
        "total_scrobbles": item.get("total_scrobbles", 0),
        "image": item.get("image", ""),
        "queried_at": item.get("queried_at", ""),
    }


def _cosmos_artist_first_listen_item(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> dict:
    return {
        "id": _artist_first_listen_item_id(username, artist),
        "type": "artist_first_listen",
        "username": username,
        "username_normalized": _normalize_lookup_value(username),
        "artist": artist,
        "artist_normalized": _normalize_lookup_value(artist),
        "first_listen_track": first_listen_track,
        "first_listen_date": first_listen_date,
        "first_listen_timestamp": first_listen_timestamp,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


def _artist_first_listen_record_from_cosmos_item(item: dict | None) -> dict | None:
    if not item:
        return None
    return {
        "id": item.get("id"),
        "username": item.get("username", ""),
        "artist": item.get("artist", ""),
        "first_listen_track": item.get("first_listen_track", ""),
        "first_listen_date": item.get("first_listen_date", ""),
        "first_listen_timestamp": item.get("first_listen_timestamp", ""),
        "queried_at": item.get("queried_at", ""),
    }


def _sqlite_init_db() -> None:
    global _INITIALIZED_SQLITE_DB_PATH

    current_db_path = _sqlite_db_path()
    if _INITIALIZED_SQLITE_DB_PATH == current_db_path:
        return

    with _SQLITE_INIT_LOCK:
        if _INITIALIZED_SQLITE_DB_PATH == current_db_path:
            return

        for attempt in range(INIT_DB_MAX_ATTEMPTS):
            try:
                with _sqlite_connect() as conn:
                    _create_sqlite_schema(conn)
                _INITIALIZED_SQLITE_DB_PATH = current_db_path
                return
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc) or attempt == INIT_DB_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(INIT_DB_RETRY_DELAY_SECONDS)


def _sqlite_get_cached(username: str, track: str, artist: str) -> dict | None:
    _sqlite_init_db()
    with _sqlite_connect() as conn:
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


def _sqlite_save_result(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> None:
    _sqlite_init_db()
    queried_at = datetime.now(timezone.utc).isoformat()
    existing = _sqlite_get_cached(username, track, artist)
    with _sqlite_connect() as conn:
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


def _sqlite_get_history(username: str) -> list[dict]:
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM searches
            WHERE LOWER(username) = LOWER(?)
            ORDER BY queried_at DESC, id DESC
            """,
            (username,),
        ).fetchall()
    return [dict(r) for r in rows]


def _sqlite_get_artist_first_listen(username: str, artist: str) -> dict | None:
    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM artist_first_listens
            WHERE LOWER(username) = LOWER(?)
              AND LOWER(artist)   = LOWER(?)
            """,
            (username, artist),
        ).fetchone()
    return dict(row) if row else None


def _sqlite_save_artist_first_listen(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> None:
    _sqlite_init_db()
    queried_at = datetime.now(timezone.utc).isoformat()
    existing = _sqlite_get_artist_first_listen(username, artist)
    with _sqlite_connect() as conn:
        if existing:
            conn.execute(
                """
                UPDATE artist_first_listens
                SET first_listen_track     = ?,
                    first_listen_date      = ?,
                    first_listen_timestamp = ?,
                    queried_at             = ?
                WHERE id = ?
                """,
                (
                    first_listen_track,
                    first_listen_date,
                    first_listen_timestamp,
                    queried_at,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO artist_first_listens
                    (username, artist, first_listen_track,
                     first_listen_date, first_listen_timestamp, queried_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    artist,
                    first_listen_track,
                    first_listen_date,
                    first_listen_timestamp,
                    queried_at,
                ),
            )
        conn.commit()


def _cosmos_init_db() -> None:
    _get_cosmos_container()


def _cosmos_get_cached(username: str, track: str, artist: str) -> dict | None:
    container = _get_cosmos_container()
    item_id = _cache_item_id(username, track, artist)
    partition_key = _normalize_lookup_value(username)
    try:
        item = container.read_item(item=item_id, partition_key=partition_key)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        return None
    return _record_from_cosmos_item(item)


def _cosmos_save_result(
    username: str,
    track: str,
    artist: str,
    album: str,
    first_listen_date: str,
    first_listen_timestamp: str,
    total_scrobbles: int,
    image: str,
) -> None:
    container = _get_cosmos_container()
    container.upsert_item(
        _cosmos_item(
            username=username,
            track=track,
            artist=artist,
            album=album,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
            total_scrobbles=total_scrobbles,
            image=image,
        )
    )


def _cosmos_get_history(username: str) -> list[dict]:
    container = _get_cosmos_container()
    query = (
        "SELECT * FROM c WHERE c.username_normalized = @username "
        "AND c.type = 'search' "
        "ORDER BY c.queried_at DESC"
    )
    items = container.query_items(
        query=query,
        parameters=[{"name": "@username", "value": _normalize_lookup_value(username)}],
        enable_cross_partition_query=False,
    )
    return [_record_from_cosmos_item(item) for item in items]


def _cosmos_get_artist_first_listen(username: str, artist: str) -> dict | None:
    container = _get_cosmos_container()
    item_id = _artist_first_listen_item_id(username, artist)
    partition_key = _normalize_lookup_value(username)
    try:
        item = container.read_item(item=item_id, partition_key=partition_key)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        return None
    return _artist_first_listen_record_from_cosmos_item(item)


def _cosmos_save_artist_first_listen(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> None:
    container = _get_cosmos_container()
    container.upsert_item(
        _cosmos_artist_first_listen_item(
            username=username,
            artist=artist,
            first_listen_track=first_listen_track,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
        )
    )


def init_db() -> None:
    """Initialize the configured persistence backend."""
    if _use_cosmos_backend():
        _cosmos_init_db()
        return
    _sqlite_init_db()


def get_cached(username: str, track: str, artist: str) -> dict | None:
    """Return the stored result for *(username, track, artist)*, or ``None``."""
    if _use_cosmos_backend():
        return _cosmos_get_cached(username, track, artist)
    return _sqlite_get_cached(username, track, artist)


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
    """Insert or update a first-listen result in the configured backend."""
    if _use_cosmos_backend():
        _cosmos_save_result(
            username=username,
            track=track,
            artist=artist,
            album=album,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
            total_scrobbles=total_scrobbles,
            image=image,
        )
        return
    _sqlite_save_result(
        username=username,
        track=track,
        artist=artist,
        album=album,
        first_listen_date=first_listen_date,
        first_listen_timestamp=first_listen_timestamp,
        total_scrobbles=total_scrobbles,
        image=image,
    )


def get_history(username: str) -> list[dict]:
    """Return all stored searches for *username*, newest first."""
    if _use_cosmos_backend():
        return _cosmos_get_history(username)
    return _sqlite_get_history(username)


def get_artist_first_listen(username: str, artist: str) -> dict | None:
    """Return the stored artist first-listen record for *(username, artist)*, or ``None``."""
    if _use_cosmos_backend():
        return _cosmos_get_artist_first_listen(username, artist)
    return _sqlite_get_artist_first_listen(username, artist)


def save_artist_first_listen(
    username: str,
    artist: str,
    first_listen_track: str,
    first_listen_date: str,
    first_listen_timestamp: str,
) -> None:
    """Insert or update an artist first-listen record in the configured backend."""
    if _use_cosmos_backend():
        _cosmos_save_artist_first_listen(
            username=username,
            artist=artist,
            first_listen_track=first_listen_track,
            first_listen_date=first_listen_date,
            first_listen_timestamp=first_listen_timestamp,
        )
        return
    _sqlite_save_artist_first_listen(
        username=username,
        artist=artist,
        first_listen_track=first_listen_track,
        first_listen_date=first_listen_date,
        first_listen_timestamp=first_listen_timestamp,
    )
