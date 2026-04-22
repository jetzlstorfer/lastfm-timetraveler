# Plan: Integrate Spotify Extended Streaming History

## TL;DR

Add the ability for users to upload their Spotify Extended Streaming History JSON files, store plays in a new SQLite table, and use that data as the **primary** source for first-listen lookups. Last.fm is an optional secondary source. Users can connect Spotify only, Last.fm only, or both (Spotify checked first). Access to uploaded Spotify data is protected by a server-generated secret token — no user can access another user's data.

---

## Auth Model: Token-Based Access Control

**Problem:** The app currently has zero authentication. Last.fm data is public (anyone can query any username). But Spotify upload data is private — each user's data must be isolated.

**Solution: Per-profile secret token**

- When a user first uploads Spotify data, the server generates a random token (`secrets.token_urlsafe(32)`) and stores it alongside the `profile_id` in a new `spotify_profiles` table.
- The token is returned to the client and stored in a cookie (`spotify_token`, `SameSite=Lax`, 365 days).
- All Spotify data endpoints (`/api/spotify/*`) require both `profile_id` and the matching token (sent as cookie or header).
- **Verification:** server looks up the profile, compares the token. Mismatch → 403.
- **No passwords, no sessions, no login forms.** The token IS the credential. Lose the cookie → re-upload your data.
- Last.fm endpoints remain unchanged (public data, no auth needed).

**New table: `spotify_profiles`**
```
profile_id    TEXT PRIMARY KEY   -- user-chosen display name
token_hash    TEXT NOT NULL      -- SHA-256 hash of the token (never store raw)
created_at    TEXT NOT NULL
```

We store the SHA-256 hash of the token (not the raw token) so a database leak doesn't expose credentials. The client sends the raw token; the server hashes it and compares.

---

## Phase 1: Backend — Data Storage, Auth & Import

### Step 1: Database changes (`database.py`)

**New table: `spotify_profiles`** (as described above)

**New table: `spotify_history`**
```
id              INTEGER PRIMARY KEY AUTOINCREMENT
profile_id      TEXT NOT NULL        -- references spotify_profiles.profile_id
track           TEXT NOT NULL
artist          TEXT NOT NULL
album           TEXT
played_at       TEXT NOT NULL        -- ISO 8601 timestamp (Spotify's `ts` field)
played_at_unix  INTEGER NOT NULL     -- for efficient MIN() queries
ms_played       INTEGER NOT NULL
```
- Unique index on `(LOWER(profile_id), played_at, LOWER(track), LOWER(artist))` to prevent duplicates on re-upload
- Index on `(LOWER(profile_id), LOWER(track), LOWER(artist))` for first-listen queries
- Index on `(LOWER(profile_id), LOWER(artist))` for artist-level queries

**New public functions:**
- `create_spotify_profile(profile_id, token_hash) -> None`
- `verify_spotify_token(profile_id, token_hash) -> bool`
- `save_spotify_plays(profile_id, plays: list[dict])` — bulk INSERT OR IGNORE
- `get_spotify_first_listen(profile_id, track, artist) -> dict | None` — MIN(played_at_unix) with track+artist+album
- `get_spotify_artist_first_listen(profile_id, artist) -> dict | None` — earliest play of any track by artist
- `get_spotify_play_count(profile_id, track, artist) -> int`
- `has_spotify_data(profile_id) -> bool`
- `clear_spotify_data(profile_id)` — deletes history rows (keeps profile)
- `delete_spotify_profile(profile_id)` — deletes profile + all history
- `search_spotify_tracks(profile_id, query) -> list[dict]` — LIKE-based search on track/artist
- `get_spotify_stats(profile_id) -> dict` — total plays, unique tracks/artists, date range

### Step 2: Auth helper & Spotify endpoints (`app.py`)

**Auth helper:**
- `_verify_spotify_access(profile_id)` — reads token from `X-Spotify-Token` header or `spotify_token` cookie, hashes with SHA-256, calls `verify_spotify_token()`. Returns True or aborts with 403.
- Applied to all `/api/spotify/*` endpoints except the initial upload (which creates the profile).

**Endpoints:**

`POST /api/spotify/upload`
- Accepts: multipart form with one or more files + `profile_id` form field. Each file may be:
  - A single `.json` file (Spotify Extended Streaming History format), **or**
  - A `.zip` archive (the raw download Spotify provides) containing JSON files at any nesting depth
- If profile doesn't exist → create it, generate token, return token in response
- If profile exists → require valid token (so only the original uploader can add data)
- **Large upload handling** (Spotify exports are commonly 20–200+ MB):
  - Set `MAX_CONTENT_LENGTH = 500 * 1024 * 1024` (500 MB) to comfortably cover multi-year exports + zips
  - Stream files from `request.files` directly — never load the full multipart body into memory at once
  - Process JSON files one at a time; use `ijson` (streaming parser) for files larger than ~25 MB so we don't hold the whole array in RAM
  - Insert plays into SQLite in batches (e.g., 1,000 rows per `executemany`) inside a single transaction per file
  - Return `413 Payload Too Large` with a clear JSON error if `MAX_CONTENT_LENGTH` is exceeded
  - Document: for ACA deployment, ensure ingress request size limit and any reverse proxy (e.g., `gunicorn --limit-request-line`, Container Apps default 100 MB) are raised to match
- **ZIP handling**:
  - Detect by extension (`.zip`) and/or magic bytes (`PK\x03\x04`)
  - Open with `zipfile.ZipFile` in streaming mode (read each entry via `.open()`, never `.extractall()` to disk)
  - **Recursively walk all entries** — Spotify's export nests JSON inside `Spotify Extended Streaming History/` and may include other folders; accept any path depth
  - Match entries by filename pattern: `Streaming_History_Audio_*.json` (case-insensitive). Also accept generic `endsong_*.json` / `StreamingHistory*.json` from older exports for forward compatibility.
  - Skip non-JSON entries (PDFs, `:Zone.Identifier` files, `__MACOSX/`, hidden dotfiles) silently
  - Guard against zip-bombs: cap total uncompressed size at e.g. 2 GB and per-entry at 500 MB; abort with 413 if exceeded
  - Reject path traversal entries (any name containing `..` or starting with `/`)
- Filter: music only (`master_metadata_track_name is not None`), `ms_played >= 30000`
- Map fields: `master_metadata_track_name` → track, `master_metadata_album_artist_name` → artist, `master_metadata_album_album_name` → album, `ts` → played_at/played_at_unix
- Return: `{ "ok": true, "token": "..." (only on first upload), "imported": N, "filtered": N, "files_processed": N, "date_range": [...], "unique_tracks": N, "unique_artists": N }`

`GET /api/spotify/status?profile_id=`
- Requires valid token
- Returns: `{ "ok": true, "has_data": bool, "stats": { total_plays, unique_tracks, unique_artists, earliest, latest } }`

`DELETE /api/spotify/data?profile_id=`
- Requires valid token
- Clears all imported history (keeps profile for re-upload)

`GET /api/spotify/search?profile_id=&q=`
- Requires valid token
- Returns: `[{ "name": track, "artist": artist, "album": album }]`
- LIKE query with `_normalize_lookup_value()` normalization, limited to 20 results, deduplicated by track+artist

### Step 3: Modify first-listen lookup (`app.py`) *depends on Step 1*

**New resolution order in `_do_first_listen_lookup()`:**
1. **Spotify check** (if `profile_id` + valid token provided): query `get_spotify_first_listen()`. If found → return immediately with `source: "spotify"` and play count from `get_spotify_play_count()`.
2. **Cache check** (existing): if Last.fm username provided, check database cache.
3. **Last.fm lookup** (existing steps 1-4): `track.getInfo` → library page scrape → recent tracks fallback.
4. **Neither found** → `{ "found": false }`

The `/api/first-listen` endpoint gains optional params: `profile_id` (for Spotify lookup). The `username` param becomes optional (only needed for Last.fm).

**Same pattern for artist first-listen:** Spotify check first, then Last.fm fallback.

**New `source` field** on all results: `"spotify"` / `"lastfm"` / `"cache"`.

---

## Phase 2: Frontend — Upload UI & Dual Connect Flow

### Step 4: Landing screen redesign (`static/index.html`)

Replace the single "Enter Last.fm username" form with a dual-path connect screen:

**Option A: "Import Spotify Data"**
- Text input for display name (profile_id)
- File picker for JSON files (multi-select, `.json` only)
- On first upload: store returned token in cookie `spotify_token` + profile_id in cookie `spotify_profile_id`
- On return visit: auto-reconnect using saved cookies (call `/api/spotify/status` to verify)

**Option B: "Connect with Last.fm"**
- Existing username input + validation flow (unchanged)

**Both connected:**
- UI shows both connection badges (e.g., "Spotify: ✓ 15,234 plays" + "Last.fm: ✓ username")
- User can disconnect either independently

**State variables to add:**
- `spotifyProfileId` — from cookie `spotify_profile_id`
- `spotifyToken` — from cookie `spotify_token`
- `hasSpotifyData` — boolean, set after status check

### Step 5: File upload UI (*depends on Step 4*)

Upload interface (shown during initial connect or accessible via settings/profile area):
- Multi-file selector button: "Select Spotify files" — accepts both `.json` and `.zip` (`accept=".json,.zip"`)
- Hint text: "Drop the ZIP Spotify emailed you, or select the individual JSON files. Large uploads (50–200 MB) are supported."
- Drag-and-drop zone supporting both file types
- Shows selected file count and total size before upload
- Upload progress: use `XMLHttpRequest` (not `fetch`) so we can hook `xhr.upload.onprogress` for a real byte-level progress bar — important since uploads can take a while
- Client-side guard: warn if total selected size exceeds 500 MB (server limit) before starting
- After upload: summary card "Imported 15,234 plays from 12 files (Sep 2010 – Apr 2026, 3,412 tracks, 891 artists)"
- "Clear & Re-import" button (calls DELETE endpoint, then allows new upload)

### Step 6: Search integration (*depends on Steps 2, 4*)

Autocomplete behavior changes based on connection state:
- **Spotify only:** call `/api/spotify/search` → show results (no album art, just track/artist/album text)
- **Last.fm only:** existing `/api/search` behavior (unchanged)
- **Both connected:** call both endpoints in parallel, merge results. Spotify results appear first (marked with subtle indicator). Deduplicate by normalized track+artist.

### Step 7: Result display updates (*depends on Step 3*)

- Source badge on result card: "📀 Found in your Spotify history" or "🎵 Found via Last.fm"
- When result comes from Spotify: skip the time-travel loading animation (result is instant, show immediately)
- When falling back to Last.fm: show loading animation as before
- Album art strategy for Spotify results: make a best-effort call to Last.fm's `track.getInfo` (public, no username needed) to fetch album art. If it fails, show a placeholder.
- When both sources have data: show the Spotify date (primary), with a note if Last.fm found a different date

---

## Phase 3: Polish

### Step 8: Listening history chart from Spotify data

Modify `/api/listening-history` to accept `profile_id` and pull from `spotify_history`:
- `SELECT strftime('%Y-%m', played_at) as month, COUNT(*) as plays FROM spotify_history WHERE ...`
- If both sources available: use Spotify data for the chart (more complete)

### Step 9: Name normalization

Reuse `normalize_lastfm_text()` (casefold + whitespace collapse) for all Spotify track/artist matching. Accept that some edge cases won't match perfectly between Spotify and Last.fm naming conventions (e.g., feat. credits, "The" prefix).

---

## Relevant Files

- `database.py` — Add `spotify_profiles` + `spotify_history` tables, auth verification, all Spotify CRUD functions. Reuse `_normalize_lookup_value()`.
- `app.py` — Add `_verify_spotify_access()` auth helper. Add upload/search/status/delete endpoints. Add ZIP extraction + streaming JSON parsing helpers. Modify `_do_first_listen_lookup()` (~line 1227) and `_find_and_store_artist_first_listen()` (~line 656) to check Spotify first. Add `MAX_CONTENT_LENGTH = 500 MB` and a 413 error handler. Make `username` optional in `/api/first-listen`.
- `requirements.txt` — Add `ijson` (streaming JSON parser for large files). `zipfile` is stdlib.
- `static/index.html` — Dual connect flow, file upload UI, search merge logic, result source indicator. Add cookie management for `spotify_token`/`spotify_profile_id`. Modify `connectUser()`, autocomplete, result rendering.
- `test_app.py` — New test classes: `TestSpotifyUpload` (import, filtering, dedup), `TestSpotifyAuth` (token verification, 403 on wrong token), `TestSpotifyFirstListen` (Spotify-first lookup, fallback to Last.fm).

## Verification

1. **Upload test**: Upload Spotify JSON → verify correct import count, podcasts/short plays excluded, re-upload produces no duplicates
2. **ZIP upload test**: Upload the raw Spotify ZIP (with nested `Spotify Extended Streaming History/*.json` layout) → verify all files discovered and imported, non-JSON entries (PDFs, `:Zone.Identifier`) ignored
3. **Large upload test**: Upload a 50+ MB file (or simulate one) → verify it completes, memory usage stays bounded (streaming parser, batched inserts), 413 returned if over `MAX_CONTENT_LENGTH`
4. **Zip-bomb / traversal test**: Upload zip containing `../evil.json` or huge inflated entry → rejected cleanly
5. **Auth test**: Request Spotify data without token → 403. Request with wrong token → 403. Request with correct token → 200.
6. **First-listen test (Spotify only)**: Upload data, search for track → returns correct earliest date with `source: "spotify"`
7. **Fallback test**: Track not in Spotify data, Last.fm username connected → falls through to Last.fm lookup, returns `source: "lastfm"`
8. **Spotify-only flow (no Last.fm)**: Connect with Spotify only, search, get results — everything works without Last.fm
9. **Manual test**: Upload actual `my_spotify_data/` files (and the original ZIP), search for a known track, verify date
10. **Cross-user isolation test**: Create two profiles, verify profile A's token can't access profile B's data

## Decisions

- **Token-based auth**: No passwords/sessions. Server generates `secrets.token_urlsafe(32)`, stores SHA-256 hash. Token sent via cookie. Simple, stateless, secure enough for this use case.
- **30-second minimum play threshold**: Matches Spotify's own counting. Plays < 30s filtered during import.
- **No album art from Spotify**: Export doesn't include images. Best-effort fetch from Last.fm public API.
- **Scope boundary**: "On This Day" and "Top Tracks" remain Last.fm-only (they rely on Last.fm API endpoints that have no Spotify equivalent).
- **SQLite only** for Spotify data in this iteration. Cosmos DB support deferred.
- **Profile ID = user-chosen display name** (not auto-generated). When both sources connected, can be anything — Last.fm username is separate.

## Further Considerations

1. **Token recovery**: If a user loses their cookie, they can't access their data — they'd need to re-upload. Could add a "recovery passphrase" flow later, but recommend keeping simple for now.
2. **Cosmos DB for Spotify data**: Deferred. Can extend the database abstraction later if needed for cloud deployment.
3. **Server-side import from local folder**: The `my_spotify_data/` folder could be auto-imported via a CLI command or admin endpoint for self-hosting. Recommend as a future enhancement.
