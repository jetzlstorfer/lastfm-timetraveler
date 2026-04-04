# Last.fm Time Traveller — Session Plan

## Project Overview
A web app that lets you find the very first time you listened to any song on Last.fm. Type a song title, get autocomplete suggestions, pick one, and see when you first scrobbled it.

## Tech Stack
- **Backend**: Python 3 / Flask
- **Frontend**: Vanilla HTML/CSS/JS (single-page, served by Flask)
- **API**: Last.fm (`track.search` for autocomplete, `user.getArtistTracks` for first-listen lookup)
- **Dev environment**: Python venv + Dev Container

## What's Done
- [x] Flask backend (`app.py`) with two API endpoints:
  - `/api/search?q=...` — autocomplete via `track.search` (returns top 8 matches)
  - `/api/first-listen?track=...&artist=...` — finds earliest scrobble via `user.getArtistTracks` pagination
- [x] Web UI (`static/index.html`) with:
  - Debounced autocomplete with keyboard navigation (↑↓ Enter Esc)
  - Result card showing: first listen date, relative time ago, total scrobble count, album art
  - Dark gradient theme, Google Fonts (Inter), animations
- [x] `.env.example` for `LASTFM_API_KEY` and `LASTFM_USERNAME`
- [x] `.gitignore` (pycache, .env, venv)
- [x] `requirements.txt` (flask, requests, python-dotenv)
- [x] Virtual environment (`.venv/`)
- [x] Dev Container config (`.devcontainer/devcontainer.json`) — Python 3.12 image, auto venv setup, port forwarding
- [x] `README.md` with setup instructions

## File Structure
```
lastfm-timetraveller/
├── .devcontainer/
│   └── devcontainer.json
├── static/
│   └── index.html          # Frontend (single file: HTML + CSS + JS)
├── .env.example             # Template for API key / username
├── .gitignore
├── app.py                   # Flask backend
├── requirements.txt
└── README.md
```

## Possible Future Work
- [x] Add error handling for missing/invalid API key (show friendly message in UI)
- [x] Show album name in result card
- [ ] Deploy to a cloud service (e.g., Fly.io, Railway)
- [ ] Add a "share" button to share the result
- [ ] Support multiple users (input username in UI instead of env var)
- [ ] Cache autocomplete results to reduce API calls
- [ ] Add rate limiting
