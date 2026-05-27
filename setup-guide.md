# GitHub ↔ Google Drive Sync — Setup Guide

## How it works
- **Push to GitHub** → files automatically copy to Google Drive
- **Manual trigger** → files pull from Google Drive back to GitHub → auto pulls to your machine

---

## One-time Setup

### 1. Google Cloud
1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a project
2. Enable **Google Drive API**
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
4. Install rclone locally → run `rclone config`:
   - Type: `drive` (option 24)
   - client_id / client_secret: leave blank
   - scope: `1` (full access)
   - Authenticate via browser when prompted
5. Run `type %APPDATA%\rclone\rclone.conf` (Windows) or `cat ~/.config/rclone/rclone.conf` (Mac/Linux)
6. Copy the `token = {...}` JSON value

### 2. Google Drive
1. Create a folder for backups
2. Copy the folder ID from the URL: `drive.google.com/drive/folders/`**`FOLDER_ID`**

### 3. GitHub Secrets
Go to repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `GDRIVE_TOKEN` | the `{...}` JSON from rclone.conf |
| `GDRIVE_FOLDER_ID` | your Drive folder ID |

### 4. GitHub Permissions
Repo → **Settings → Actions → General → Workflow permissions** → select **Read and write permissions**

### 5. Workflow Files
Add these to `.github/workflows/` in your repo:

**`backup-to-gdrive.yml`** — runs on every push to main
**`pull-from-gdrive.yml`** — runs manually

> Replace `root_folder_id` in both files with your folder ID, or use `${{ secrets.GDRIVE_FOLDER_ID }}` if stored as a secret.

### 6. VS Code
1. Install extension: **GitHub Actions** (`GitHub.vscode-github-actions`)
2. Install **GitHub CLI** from [cli.github.com](https://cli.github.com) → run `gh auth login` once
3. Add `.vscode/tasks.json` to your repo

---

## Daily Use

| Action | How |
|---|---|
| **Backup** | Just push to GitHub — workflow fires automatically |
| **Restore** | `Ctrl+Shift+P` → Tasks: Run Task → **Restore from Google Drive** |
| **Restore (terminal)** | `gh workflow run pull-from-gdrive.yml --ref main --field confirm=YES` then `git pull` |

---

## Changing Things Later

| What | What to update |
|---|---|
| GitHub repo | Nothing — yml uses repo name automatically. Copy `.github/workflows/` to new repo |
| Google Drive folder | Update `root_folder_id` in both yml files (or update `GDRIVE_FOLDER_ID` secret) |
| Google account | Re-run `rclone config`, get new token, update `GDRIVE_TOKEN` secret |

---

## File Rules (current config)

| File type | Backup (push) | Restore (pull) |
|---|---|---|
| All code files | ✅ copied to Drive | ✅ pulled from Drive |
| `.md` files | ❌ excluded | ❌ excluded |
| `README.md` | ❌ excluded | ✅ included |
| `.git/**` | ❌ excluded | ❌ excluded |
| `.github/**` | ❌ excluded | ❌ excluded |
| Google Docs/Sheets in Drive | — | ❌ skipped |
| Extra files only in Drive | — | 🔒 untouched |

---

## Secrets Reference

| Secret | Description |
|---|---|
| `GDRIVE_TOKEN` | OAuth token JSON from rclone config |
| `GDRIVE_FOLDER_ID` | Google Drive backup folder ID |
| `GITHUB_TOKEN` | Auto-provided by GitHub Actions — do not add manually |
