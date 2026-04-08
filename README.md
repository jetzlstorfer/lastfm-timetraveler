# 🕰️🧑‍🚀 Last.fm Time Traveler

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

### Running tests

```bash
make test
```

or directly:

```bash
pytest
```

## Deploy to Azure

This project includes everything needed to deploy to **Azure Container Apps** using the [Azure Developer CLI (azd)](https://aka.ms/azd).

### Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [Azure Developer CLI (azd)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- An Azure subscription

### One-command deployment

```bash
azd up
```

This single command will:
1. Build and push the Docker image to **Azure Container Registry**
2. Provision all infrastructure (**Container Apps Environment**, **Container App**, **Log Analytics**, **Azure Cosmos DB for NoSQL**)
3. Deploy the application to **Azure Container Apps**

The resource group, Container App, Container Apps environment, and Log Analytics workspace names are derived from `AZURE_ENV_NAME`. The Azure Container Registry name also uses `AZURE_ENV_NAME`, with a short stable hash suffix because registry names must be globally unique and alphanumeric.

With `lastfm-timetraveler`, the default app URL will be similar to:

```text
https://ca-lastfm-timetraveler.<managed-environment-suffix>.swedencentral.azurecontainerapps.io
```

Azure Container Apps always includes the managed environment suffix in its default hostname. If you need a URL without that extra segment, configure a custom domain.

You will be prompted for:
- `AZURE_ENV_NAME` — a short name for this environment (e.g. `lastfm-prod`)
- `AZURE_LOCATION` — Azure region (e.g. `eastus`)
- `LASTFM_API_KEY` — your Last.fm API key (stored as a secret)

### CI/CD with GitHub Actions

The `.github/workflows/azure-aca-deploy.yml` workflow runs `azd provision` and `azd deploy` automatically on every push to `main`.

**Required GitHub repository variables** (`Settings → Secrets and variables → Actions`):

| Name | Kind | Description |
|------|------|-------------|
| `AZURE_CLIENT_ID` | Variable | App registration client ID (for OIDC) |
| `AZURE_TENANT_ID` | Variable | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Variable | Azure subscription ID |
| `AZURE_ENV_NAME` | Variable | azd environment name |
| `AZURE_LOCATION` | Variable | Azure region (e.g. `eastus`) |
| `LASTFM_API_KEY` | **Secret** | Last.fm API key |

To set up federated credentials (OIDC) for the service principal, follow the [azd GitHub Actions guide](https://learn.microsoft.com/azure/developer/azure-developer-cli/configure-devops-pipeline).

[Dependabot](.github/dependabot.yml) is configured to open weekly PRs for pip, Docker, and GitHub Actions dependency updates.

## How it works

- **Autocomplete** uses Last.fm's `track.search` API
- **First listen** fetches `track.getInfo` for the play count, then scrapes the user's public library page for the oldest visible scrobble date. If the public page is unavailable (private profile, login wall, etc.) it falls back to scanning `user.getRecentTracks` pages backward from oldest to newest
- **Caching** — confirmed lookups are written to Azure Cosmos DB in Azure, or to a local SQLite file by default during development
- **History** — the `/api/history` endpoint returns cached lookups for a given user, including partial results whose exact first-listen date could not be resolved yet
- Built with **Flask** (backend) and vanilla **HTML/CSS/JS** (frontend)

### Database

The app supports two persistence modes:

- **Default local mode** — uses a SQLite file (`timetraveler.db` by default, overridable with `DB_PATH`)
- **Cosmos mode** — enabled when `COSMOS_CONNECTION_STRING` is set (or both `COSMOS_ENDPOINT` and `COSMOS_KEY`); the app then uses the configured Cosmos database and container instead of SQLite

To run the Cosmos path locally, start the Azure Cosmos DB emulator in Docker and point the app at it:

```bash
docker pull mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-preview
docker run -d -p 8081:8081 -p 1234:1234 mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-preview

export COSMOS_CONNECTION_STRING='AccountEndpoint=http://localhost:8081/;AccountKey=<emulator-key>;'
export COSMOS_DATABASE_NAME='lastfm-timetraveler'
export COSMOS_CONTAINER_NAME='searches'
python app.py
```

The emulator exposes the database endpoint on `http://localhost:8081` and the local data explorer on `http://localhost:1234`.

| Endpoint | Description |
|---|---|
| `GET /api/status` | Health check — verifies the API key is configured |
| `GET /api/ready` | Readiness probe — tests API key and database connectivity |
| `GET /api/user/validate?username=` | Validates a Last.fm username and returns profile info |
| `GET /api/user/top-tracks?username=&period=` | Top tracks for a user (`7day`, `1month`, `3month`, `6month`, `12month`, `overall`) |
| `GET /api/on-this-day?username=` | What the user listened to on this day 1, 2, 3, 5, and 10 years ago |
| `GET /api/search?q=` | Track autocomplete search (minimum 2 characters) |
| `GET /api/first-listen?track=&artist=&username=` | Find the first scrobble (returns `202` with a `lookup_id` for async polling) |
| `GET /api/lookup-progress?lookup_id=` | Poll progress of an async first-listen lookup |
| `GET /api/artist-image?artist=` | Fetch an image URL for an artist |
| `GET /api/history?username=` | All cached first-listen results for the given username |
