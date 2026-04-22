"""Persistence layer for Last.fm Time Traveler.

The app supports two storage backends:

- SQLite for local and test workflows.
- Azure Cosmos DB for NoSQL when Cosmos environment variables are configured.

The public API remains the same so the rest of the Flask app can treat the
cache as a simple key-value store with history queries by username.
"""

import hashlib
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
COSMOS_SPOTIFY_PROFILES_CONTAINER = "spotify_profiles"
COSMOS_SPOTIFY_PLAYS_CONTAINER = "spotify_plays"
SPOTIFY_BULK_INSERT_WORKERS = int(os.getenv("SPOTIFY_BULK_INSERT_WORKERS", "16"))
# How often (seconds) to refresh a profile's TTL on access. The Cosmos
# container has a 90-day TTL; touching less often than once a day would risk
# expiry, more often is wasted writes.
SPOTIFY_TOUCH_INTERVAL_SECONDS = int(os.getenv("SPOTIFY_TOUCH_INTERVAL_SECONDS", "3600"))
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
_COSMOS_EXTRA_CONTAINERS: dict[str, object] = {}


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spotify_profiles (
            profile_id   TEXT PRIMARY KEY,
            token_hash   TEXT NOT NULL,
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spotify_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id      TEXT    NOT NULL,
            track           TEXT    NOT NULL,
            artist          TEXT    NOT NULL,
            album           TEXT,
            played_at       TEXT    NOT NULL,
            played_at_unix  INTEGER NOT NULL,
            ms_played       INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_spotify_history_dedup
        ON spotify_history (LOWER(profile_id), played_at, LOWER(track), LOWER(artist))
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_spotify_history_track_artist
        ON spotify_history (LOWER(profile_id), LOWER(track), LOWER(artist))
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_spotify_history_artist
        ON spotify_history (LOWER(profile_id), LOWER(artist))
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


def _get_cosmos_named_container(container_name: str, partition_key_path: str):
    """Return (and lazily create) a Cosmos container alongside the primary one."""
    # Ensure the primary client/database is initialized first.
    _get_cosmos_container()
    with _COSMOS_INIT_LOCK:
        cached = _COSMOS_EXTRA_CONTAINERS.get(container_name)
        if cached is not None:
            return cached
        if _COSMOS_DATABASE is None:
            raise RuntimeError("Cosmos database is not initialized")
        container = _COSMOS_DATABASE.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path=partition_key_path),
        )
        _COSMOS_EXTRA_CONTAINERS[container_name] = container
        return container


def _spotify_profiles_container():
    return _get_cosmos_named_container(COSMOS_SPOTIFY_PROFILES_CONTAINER, "/profile_id_normalized")


def _spotify_plays_container():
    return _get_cosmos_named_container(COSMOS_SPOTIFY_PLAYS_CONTAINER, "/profile_id_normalized")


def _spotify_play_doc_id(profile_id: str, played_at: str, track: str, artist: str) -> str:
    """Deterministic doc id matches the SQLite dedup index semantics."""
    raw = "|".join([
        _normalize_lookup_value(profile_id),
        played_at or "",
        _normalize_lookup_value(track),
        _normalize_lookup_value(artist),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _spotify_profile_doc(profile_id: str, token_hash: str) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "id": _normalize_lookup_value(profile_id),
        "type": "spotify_profile",
        "profile_id": profile_id,
        "profile_id_normalized": _normalize_lookup_value(profile_id),
        "token_hash": token_hash,
        "created_at": now_iso,
        "last_accessed_at": now_iso,
    }


def _spotify_play_doc(profile_id: str, play: dict) -> dict:
    track = play["track"]
    artist = play["artist"]
    played_at = play["played_at"]
    return {
        "id": _spotify_play_doc_id(profile_id, played_at, track, artist),
        "type": "spotify_play",
        "profile_id": profile_id,
        "profile_id_normalized": _normalize_lookup_value(profile_id),
        "track": track,
        "track_normalized": _normalize_lookup_value(track),
        "artist": artist,
        "artist_normalized": _normalize_lookup_value(artist),
        "album": play.get("album", "") or "",
        "played_at": played_at,
        "played_at_unix": int(play["played_at_unix"]),
        "ms_played": int(play.get("ms_played", 0) or 0),
    }


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


# ---------------------------------------------------------------------------
# Spotify Extended Streaming History
#
# Storage: SQLite by default; Azure Cosmos DB when configured (see
# `_use_cosmos_backend`). Cosmos uses two containers:
#   - `spotify_profiles`  (partition key: /profile_id_normalized)
#   - `spotify_plays`     (partition key: /profile_id_normalized)
# Play documents have a deterministic SHA-1 `id` (see `_spotify_play_doc_id`)
# so re-uploads are naturally deduplicated via 409 Conflict.
# ---------------------------------------------------------------------------


def create_spotify_profile(profile_id: str, token_hash: str) -> None:
    """Create a new Spotify profile. Raises if it already exists."""
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        try:
            container.create_item(body=_spotify_profile_doc(profile_id, token_hash))
        except cosmos_exceptions.CosmosResourceExistsError as exc:
            raise ValueError(f"Spotify profile already exists: {profile_id}") from exc
        return

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute(
            "INSERT INTO spotify_profiles (profile_id, token_hash, created_at) VALUES (?, ?, ?)",
            (profile_id, token_hash, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def spotify_profile_exists(profile_id: str) -> bool:
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            container.read_item(item=normalized, partition_key=normalized)
            return True
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return False

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM spotify_profiles WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()
    return row is not None


def spotify_profile_was_accessed(profile_id: str) -> bool:
    """Return True if the profile has ever been successfully verified.

    Used to detect the "504 orphan" scenario: when a previous upload created
    the profile and issued a token but the response never reached the client
    (e.g. ingress timeout), the profile exists but no one ever proved
    ownership. In that case it's safe to let a fresh upload re-claim it.

    A profile counts as accessed iff `last_accessed_at` is present and
    strictly later than `created_at`. The `verify_spotify_token` Cosmos
    branch is responsible for keeping `last_accessed_at` fresh on every
    authenticated request.

    SQLite profiles are always considered accessed (the SQLite backend
    doesn't track this and the local-dev workflow doesn't have the
    timeout-orphan problem).
    """
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            item = container.read_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return False
        last = (item.get("last_accessed_at") or "").strip()
        created = (item.get("created_at") or "").strip()
        if not last:
            return False
        return last != created

    return True


def verify_spotify_token(profile_id: str, token_hash: str) -> bool:
    """Return True iff *(profile_id, token_hash)* matches a stored profile.

    On a successful Cosmos verification, this also refreshes the profile's
    `last_accessed_at` (and TTL) at most once per `SPOTIFY_TOUCH_INTERVAL_SECONDS`,
    so active users never lose their data to the 90-day TTL.
    """
    if not profile_id or not token_hash:
        return False
    if _use_cosmos_backend():
        container = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            item = container.read_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return False
        if (item.get("token_hash") or "") != token_hash:
            return False
        _maybe_touch_spotify_profile(container, item)
        return True

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT token_hash FROM spotify_profiles WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()
    if not row:
        return False
    return (row["token_hash"] or "") == token_hash


def _maybe_touch_spotify_profile(container, item: dict) -> None:
    """Refresh `last_accessed_at` on the profile doc to extend its TTL.

    Throttled by `SPOTIFY_TOUCH_INTERVAL_SECONDS` so we don't write on every
    request. Failures are silently swallowed — the worst case is the profile
    expires after 90 days of inactivity, which is the desired behavior anyway.
    """
    now = datetime.now(timezone.utc)
    last = item.get("last_accessed_at") or item.get("created_at") or ""
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if (now - last_dt).total_seconds() < SPOTIFY_TOUCH_INTERVAL_SECONDS:
                return
        except ValueError:
            pass
    item["last_accessed_at"] = now.isoformat()
    try:
        container.upsert_item(body=item)
    except Exception:  # noqa: BLE001 — best-effort, never block the request
        pass


def save_spotify_plays(profile_id: str, plays: list[dict]) -> int:
    """Bulk-insert Spotify plays. Returns the number of newly-inserted rows.

    Each *play* dict must have keys: track, artist, album, played_at,
    played_at_unix, ms_played. Existing rows are ignored (deterministic id).
    """
    if not plays:
        return 0

    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        # Count existing plays so we can report how many are *new* this upload.
        # Re-uploads still upsert every doc, which refreshes their 90-day TTL.
        before_items = list(container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        before = int(before_items[0]) if before_items else 0

        docs = [_spotify_play_doc(profile_id, p) for p in plays]

        def _upsert_one(doc):
            try:
                container.upsert_item(body=doc)
            except cosmos_exceptions.CosmosHttpResponseError:
                # SDK already retries 429s; surface other errors as a skip.
                return False
            return True

        with ThreadPoolExecutor(max_workers=max(1, SPOTIFY_BULK_INSERT_WORKERS)) as ex:
            list(ex.map(_upsert_one, docs))

        after_items = list(container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        after = int(after_items[0]) if after_items else before
        return max(0, after - before)

    _sqlite_init_db()
    rows = [
        (
            profile_id,
            p["track"],
            p["artist"],
            p.get("album", "") or "",
            p["played_at"],
            int(p["played_at_unix"]),
            int(p.get("ms_played", 0) or 0),
        )
        for p in plays
    ]
    with _sqlite_connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()["c"]
        conn.executemany(
            """
            INSERT OR IGNORE INTO spotify_history
                (profile_id, track, artist, album, played_at, played_at_unix, ms_played)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        ).fetchone()["c"]
    return int(after) - int(before)


def has_spotify_data(profile_id: str) -> bool:
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query="SELECT TOP 1 c.id FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        return bool(items)

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM spotify_history WHERE LOWER(profile_id) = LOWER(?) LIMIT 1",
            (profile_id,),
        ).fetchone()
    return row is not None


def get_spotify_first_listen(profile_id: str, track: str, artist: str) -> dict | None:
    """Return the earliest play of *(track, artist)* for the profile, or None."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT TOP 1 c.track, c.artist, c.album, c.played_at, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p "
                "AND c.track_normalized = @t AND c.artist_normalized = @a "
                "ORDER BY c.played_at_unix ASC"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@t", "value": _normalize_lookup_value(track)},
                {"name": "@a", "value": _normalize_lookup_value(artist)},
            ],
            partition_key=normalized,
        ))
        return items[0] if items else None

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT track, artist, album, played_at, played_at_unix
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND LOWER(track)      = LOWER(?)
              AND LOWER(artist)     = LOWER(?)
            ORDER BY played_at_unix ASC
            LIMIT 1
            """,
            (profile_id, track, artist),
        ).fetchone()
    return dict(row) if row else None


def get_spotify_artist_first_listen(profile_id: str, artist: str) -> dict | None:
    """Return the earliest play of any track by *artist* for the profile."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT TOP 1 c.track, c.artist, c.album, c.played_at, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p AND c.artist_normalized = @a "
                "ORDER BY c.played_at_unix ASC"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@a", "value": _normalize_lookup_value(artist)},
            ],
            partition_key=normalized,
        ))
        return items[0] if items else None

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT track, artist, album, played_at, played_at_unix
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND LOWER(artist)     = LOWER(?)
            ORDER BY played_at_unix ASC
            LIMIT 1
            """,
            (profile_id, artist),
        ).fetchone()
    return dict(row) if row else None


def get_spotify_play_count(profile_id: str, track: str, artist: str) -> int:
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        items = list(container.query_items(
            query=(
                "SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p "
                "AND c.track_normalized = @t AND c.artist_normalized = @a"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@t", "value": _normalize_lookup_value(track)},
                {"name": "@a", "value": _normalize_lookup_value(artist)},
            ],
            partition_key=normalized,
        ))
        return int(items[0]) if items else 0

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND LOWER(track)      = LOWER(?)
              AND LOWER(artist)     = LOWER(?)
            """,
            (profile_id, track, artist),
        ).fetchone()
    return int(row["c"] or 0)


def clear_spotify_data(profile_id: str) -> int:
    """Delete all imported plays for *profile_id*. Returns rows deleted."""
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        # Count first; the partition-key delete returns no count.
        count_items = list(container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.profile_id_normalized = @p",
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        count = int(count_items[0]) if count_items else 0
        if count == 0:
            return 0
        # Try the bulk partition-key delete first (fast, single request). It
        # may not be available on the SDK (AttributeError) or may be disabled
        # at the account level (CosmosHttpResponseError 400/403). Fall back to
        # per-item deletes in either case so the operation always succeeds.
        bulk_ok = False
        try:
            container.delete_all_items_by_partition_key(normalized)
            bulk_ok = True
        except AttributeError:
            pass
        except cosmos_exceptions.CosmosHttpResponseError:
            pass
        if not bulk_ok:
            ids = list(container.query_items(
                query="SELECT c.id FROM c WHERE c.profile_id_normalized = @p",
                parameters=[{"name": "@p", "value": normalized}],
                partition_key=normalized,
            ))
            for item in ids:
                try:
                    container.delete_item(item=item["id"], partition_key=normalized)
                except cosmos_exceptions.CosmosResourceNotFoundError:
                    pass
        return count

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        cur = conn.execute(
            "DELETE FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        )
        conn.commit()
        return cur.rowcount or 0


def delete_spotify_profile(profile_id: str) -> None:
    if _use_cosmos_backend():
        clear_spotify_data(profile_id)
        profiles = _spotify_profiles_container()
        normalized = _normalize_lookup_value(profile_id)
        try:
            profiles.delete_item(item=normalized, partition_key=normalized)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            pass
        return

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        conn.execute(
            "DELETE FROM spotify_history WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        )
        conn.execute(
            "DELETE FROM spotify_profiles WHERE LOWER(profile_id) = LOWER(?)",
            (profile_id,),
        )
        conn.commit()


def search_spotify_tracks(profile_id: str, query: str, limit: int = 20) -> list[dict]:
    """LIKE-based autocomplete search over imported Spotify tracks."""
    if not query or not query.strip():
        return []
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        needle = _normalize_lookup_value(query)
        # GROUP BY in Cosmos doesn't support ORDER BY on aggregates without
        # composite indexes; do the ranking client-side over a bounded result set.
        items = list(container.query_items(
            query=(
                "SELECT c.track, c.artist, c.album, c.track_normalized, "
                "c.artist_normalized, c.played_at_unix "
                "FROM c WHERE c.profile_id_normalized = @p "
                "AND (CONTAINS(c.track_normalized, @q) OR CONTAINS(c.artist_normalized, @q)) "
                "ORDER BY c.played_at_unix DESC OFFSET 0 LIMIT 5000"
            ),
            parameters=[
                {"name": "@p", "value": normalized},
                {"name": "@q", "value": needle},
            ],
            partition_key=normalized,
        ))
        agg: dict[tuple[str, str], dict] = {}
        for it in items:
            key = (it.get("track_normalized", ""), it.get("artist_normalized", ""))
            existing = agg.get(key)
            played = int(it.get("played_at_unix") or 0)
            if existing is None:
                agg[key] = {
                    "track": it.get("track", ""),
                    "artist": it.get("artist", ""),
                    "album": it.get("album", ""),
                    "first_played": played,
                    "_count": 1,
                }
            else:
                existing["_count"] += 1
                if played < existing["first_played"]:
                    existing["first_played"] = played
        ranked = sorted(agg.values(), key=lambda d: d["_count"], reverse=True)[: int(limit)]
        for r in ranked:
            r.pop("_count", None)
        return ranked

    _sqlite_init_db()
    pattern = f"%{_normalize_lookup_value(query)}%"
    with _sqlite_connect() as conn:
        rows = conn.execute(
            """
            SELECT track, artist, album, MIN(played_at_unix) AS first_played
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
              AND (LOWER(track) LIKE ? OR LOWER(artist) LIKE ?)
            GROUP BY LOWER(track), LOWER(artist)
            ORDER BY COUNT(*) DESC
            LIMIT ?
            """,
            (profile_id, pattern, pattern, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_spotify_stats(profile_id: str) -> dict:
    if _use_cosmos_backend():
        container = _spotify_plays_container()
        normalized = _normalize_lookup_value(profile_id)
        agg = list(container.query_items(
            query=(
                "SELECT VALUE { total: COUNT(1), earliest: MIN(c.played_at), "
                "latest: MAX(c.played_at) } FROM c WHERE c.profile_id_normalized = @p"
            ),
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        totals = agg[0] if agg else {}
        # Distinct counts via GROUP BY (one row per distinct value).
        track_groups = list(container.query_items(
            query=(
                "SELECT c.track_normalized, c.artist_normalized FROM c "
                "WHERE c.profile_id_normalized = @p "
                "GROUP BY c.track_normalized, c.artist_normalized"
            ),
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        artist_groups = list(container.query_items(
            query=(
                "SELECT c.artist_normalized FROM c "
                "WHERE c.profile_id_normalized = @p "
                "GROUP BY c.artist_normalized"
            ),
            parameters=[{"name": "@p", "value": normalized}],
            partition_key=normalized,
        ))
        return {
            "total_plays": int(totals.get("total") or 0),
            "unique_tracks": len(track_groups),
            "unique_artists": len(artist_groups),
            "earliest": totals.get("earliest") or "",
            "latest": totals.get("latest") or "",
        }

    _sqlite_init_db()
    with _sqlite_connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                           AS total_plays,
                COUNT(DISTINCT LOWER(track) || '|' || LOWER(artist)) AS unique_tracks,
                COUNT(DISTINCT LOWER(artist))                      AS unique_artists,
                MIN(played_at)                                     AS earliest,
                MAX(played_at)                                     AS latest
            FROM spotify_history
            WHERE LOWER(profile_id) = LOWER(?)
            """,
            (profile_id,),
        ).fetchone()
    return {
        "total_plays": int(row["total_plays"] or 0),
        "unique_tracks": int(row["unique_tracks"] or 0),
        "unique_artists": int(row["unique_artists"] or 0),
        "earliest": row["earliest"] or "",
        "latest": row["latest"] or "",
    }


