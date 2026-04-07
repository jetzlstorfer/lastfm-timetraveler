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
2. Provision all infrastructure (**Container Apps Environment**, **Container App**, **Log Analytics**)
3. Deploy the application to **Azure Container Apps**

The Container App name is derived from `AZURE_ENV_NAME`, so with `lastfm-timetraveler` the default app URL will be similar to:

```text
https://ca-lastfm-timetraveler.<managed-environment-suffix>.swedencentral.azurecontainerapps.io
```

Azure Container Apps always includes the managed environment suffix in its default hostname. If you need a URL without that extra segment, configure a custom domain.

You will be prompted for:
- `AZURE_ENV_NAME` — a short name for this environment (e.g. `lastfm-prod`)
- `AZURE_LOCATION` — Azure region (e.g. `eastus`)
- `LASTFM_API_KEY` — your Last.fm API key (stored as a secret)

### CI/CD with GitHub Actions

The `.github/workflows/azure-dev.yml` workflow runs `azd provision` and `azd deploy` automatically on every push to `main`.

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

## How it works

- **Autocomplete** uses Last.fm's `track.search` API
- **First listen** uses a binary search over `user.getWeeklyTrackChart` to locate the earliest week, then `user.getRecentTracks` to find the exact scrobble date
- **Caching** — resolved first-listen results are stored in a local SQLite database (`timetraveler.db`). Repeated queries for the same track are served instantly from the cache without hitting the Last.fm API again
- **History** — the `/api/history` endpoint returns all previously resolved lookups for the configured user
- Built with **Flask** (backend) and vanilla **HTML/CSS/JS** (frontend)

### Database

The app creates `timetraveler.db` automatically on first run. The path can be overridden with the `DB_PATH` environment variable (useful for testing or custom deployments).

| Endpoint | Description |
|---|---|
| `GET /api/history` | All cached first-listen results for the configured user |
| `GET /api/history?username=<user>` | Results for a specific username |
