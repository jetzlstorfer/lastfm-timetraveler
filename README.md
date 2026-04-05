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

You will be prompted for:
- `AZURE_ENV_NAME` — a short name for this environment (e.g. `lastfm-prod`)
- `AZURE_LOCATION` — Azure region (e.g. `eastus`)
- `LASTFM_API_KEY` — your Last.fm API key (stored as a secret)
- `LASTFM_USERNAME` — your Last.fm username

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
| `LASTFM_USERNAME` | Variable | Last.fm username |
| `LASTFM_API_KEY` | **Secret** | Last.fm API key |

To set up federated credentials (OIDC) for the service principal, follow the [azd GitHub Actions guide](https://learn.microsoft.com/azure/developer/azure-developer-cli/configure-devops-pipeline).

## How it works

- **Autocomplete** uses Last.fm's `track.search` API
- **First listen** uses `user.getArtistTracks` to paginate to the oldest scrobble of the selected track
- Built with **Flask** (backend) and vanilla **HTML/CSS/JS** (frontend)
