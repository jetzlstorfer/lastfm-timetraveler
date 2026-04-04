# 🕰️ Last.fm Time Traveler

Find the very first time you listened to any song on Last.fm.

Type a song title, pick from the autocomplete suggestions, and discover when you first scrobbled it — plus how many times you've listened since.

![Screenshot](https://img.shields.io/badge/Python-Flask-blue)

## Setup

1. **Get a Last.fm API key** at https://www.last.fm/api/account/create

2. **Configure environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your API key and Last.fm username
   ```

3. **Install & run**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```

4. Open http://localhost:5000

### Dev Container

Open this repo in VS Code with the Dev Containers extension — it will auto-create the venv and install dependencies.

## How it works

- **Autocomplete** uses Last.fm's `track.search` API
- **First listen** uses `user.getArtistTracks` to paginate to the oldest scrobble of the selected track
- Built with **Flask** (backend) and vanilla **HTML/CSS/JS** (frontend)
