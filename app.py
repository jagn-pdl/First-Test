"""
DriveSync — Local desktop sync app
Bridges: Windows PC ↔ GitHub ↔ Google Drive
Run: python app.py  →  opens http://localhost:5000
"""

import os
import io
import json
import base64
import shutil
import subprocess
import threading
import webbrowser
import time
from pathlib import Path
from flask import Flask, render_template, request, jsonify, redirect, url_for, session

# ── Google Drive ──────────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── GitHub ────────────────────────────────────────────────────────────────────
from github import Github, GithubException

app = Flask(__name__)
app.secret_key = os.urandom(24)

CONFIG_FILE = Path("config.json")
SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_REDIRECT_URI = "http://localhost:5000/oauth2callback"

# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {
        "local_path": "",
        "github_token": "",
        "github_repo": "",
        "github_branch": "main",
        "drive_folder_id": "",
        "drive_folder_name": "",
        "google_credentials_file": "credentials.json",
        "google_token": None,
    }

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

# ─────────────────────────────────────────────────────────────────────────────
# Google Drive helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_drive_creds(cfg):
    token_data = cfg.get("google_token")
    if not token_data:
        return None
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            cfg["google_token"] = json.loads(creds.to_json())
            save_config(cfg)
        except Exception:
            return None
    return creds if creds.valid else None

def get_drive_service(cfg):
    creds = get_drive_creds(cfg)
    if not creds:
        return None
    return build("drive", "v3", credentials=creds)

GOOGLE_EXPORT_MAP = {
    "application/vnd.google-apps.document":     ("text/plain",   ".txt"),
    "application/vnd.google-apps.spreadsheet":  ("text/csv",     ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain",   ".txt"),
}
SKIP_MIME = {"application/vnd.google-apps.folder"}

def list_drive_recursive(service, folder_id, prefix=""):
    """Return list of (relative_path, file_id, mime_type)."""
    results = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType)",
            pageToken=page_token,
        ).execute()
        for item in resp.get("files", []):
            rel = f"{prefix}/{item['name']}" if prefix else item["name"]
            if item["mimeType"] == "application/vnd.google-apps.folder":
                results.extend(list_drive_recursive(service, item["id"], rel))
            elif item["mimeType"] not in SKIP_MIME:
                results.append((rel, item["id"], item["mimeType"]))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results

def download_drive_file(service, file_id, mime_type):
    if mime_type in GOOGLE_EXPORT_MAP:
        export_mime, _ = GOOGLE_EXPORT_MAP[mime_type]
        req = service.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()

def upload_file_to_drive(service, local_path, drive_folder_id, drive_path):
    """Upload or update a file in Drive, creating sub-folders as needed."""
    parts = Path(drive_path).parts
    parent_id = drive_folder_id

    # Create intermediate folders
    for folder_name in parts[:-1]:
        resp = service.files().list(
            q=f"'{parent_id}' in parents and name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id)"
        ).execute()
        if resp.get("files"):
            parent_id = resp["files"][0]["id"]
        else:
            folder_meta = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
            created = service.files().create(body=folder_meta, fields="id").execute()
            parent_id = created["id"]

    file_name = parts[-1]
    # Check if file exists
    existing = service.files().list(
        q=f"'{parent_id}' in parents and name='{file_name}' and trashed=false",
        fields="files(id)"
    ).execute().get("files", [])

    media = MediaFileUpload(str(local_path), resumable=False)
    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media).execute()
    else:
        meta = {"name": file_name, "parents": [parent_id]}
        service.files().create(body=meta, media_body=media, fields="id").execute()

# ─────────────────────────────────────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────────────────────────────────────

def git(local_path, *args, env=None):
    """Run a git command in local_path, return (stdout, stderr, returncode)."""
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    r = subprocess.run(
        ["git"] + list(args),
        cwd=local_path,
        capture_output=True,
        text=True,
        env=full_env,
    )
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def ensure_git_repo(local_path, cfg):
    """Init git repo and set remote if needed."""
    repo_dir = Path(local_path) / ".git"
    if not repo_dir.exists():
        git(local_path, "init", "-b", cfg["github_branch"])

    # Set remote
    _, _, rc = git(local_path, "remote", "get-url", "origin")
    repo_url = f"https://{cfg['github_token']}@github.com/{cfg['github_repo']}.git"
    if rc != 0:
        git(local_path, "remote", "add", "origin", repo_url)
    else:
        git(local_path, "remote", "set-url", "origin", repo_url)

    # Config user
    git(local_path, "config", "user.email", "drivesync@local")
    git(local_path, "config", "user.name", "DriveSync")

# ─────────────────────────────────────────────────────────────────────────────
# Status helpers
# ─────────────────────────────────────────────────────────────────────────────

def auth_status(cfg):
    drive_ok = get_drive_creds(cfg) is not None
    github_ok = False
    if cfg.get("github_token"):
        try:
            g = Github(cfg["github_token"])
            g.get_user().login
            github_ok = True
        except Exception:
            pass
    return drive_ok, github_ok

# ─────────────────────────────────────────────────────────────────────────────
# Routes — UI
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    cfg = load_config()
    drive_ok, github_ok = auth_status(cfg)
    return render_template("index.html",
        cfg=cfg,
        drive_ok=drive_ok,
        github_ok=github_ok,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Config save
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/save_config", methods=["POST"])
def save_config_route():
    cfg = load_config()
    data = request.json
    for key in ["local_path", "github_token", "github_repo", "github_branch",
                "drive_folder_id", "drive_folder_name", "google_credentials_file"]:
        if key in data:
            cfg[key] = data[key]
    save_config(cfg)
    drive_ok, github_ok = auth_status(cfg)
    return jsonify({"ok": True, "drive_ok": drive_ok, "github_ok": github_ok})

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Google OAuth
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/google_auth")
def google_auth():
    cfg = load_config()
    creds_file = cfg.get("google_credentials_file", "credentials.json")
    if not Path(creds_file).exists():
        return jsonify({"error": f"credentials.json not found at: {Path(creds_file).absolute()}"}), 400
    flow = Flow.from_client_secrets_file(creds_file, scopes=SCOPES, redirect_uri=GOOGLE_REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route("/oauth2callback")
def oauth2callback():
    cfg = load_config()
    creds_file = cfg.get("google_credentials_file", "credentials.json")
    flow = Flow.from_client_secrets_file(
        creds_file, scopes=SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
        state=session.get("oauth_state"),
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    cfg["google_token"] = json.loads(creds.to_json())
    save_config(cfg)
    return redirect("/")

# ─────────────────────────────────────────────────────────────────────────────
# Routes — GitHub validate
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/validate_github", methods=["POST"])
def validate_github():
    data = request.json
    token = data.get("token", "")
    try:
        g = Github(token)
        user = g.get_user()
        login = user.login
        repos = [r.full_name for r in user.get_repos()][:50]
        return jsonify({"ok": True, "login": login, "repos": repos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Drive folder browser
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/list_drive_folders")
def list_drive_folders():
    cfg = load_config()
    service = get_drive_service(cfg)
    if not service:
        return jsonify({"error": "Not authenticated with Google Drive"}), 401

    parent = request.args.get("parent", "root")
    try:
        resp = service.files().list(
            q=f"'{parent}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)",
            orderBy="name",
        ).execute()
        folders = [{"id": f["id"], "name": f["name"]} for f in resp.get("files", [])]
        return jsonify({"folders": folders})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# Routes — Local folder browse (Windows)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/browse_local")
def browse_local():
    path = request.args.get("path", "")
    if not path:
        # List drives on Windows
        import string
        drives = []
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if Path(d).exists():
                drives.append({"name": d, "path": d})
        return jsonify({"items": drives, "current": ""})

    p = Path(path)
    if not p.exists() or not p.is_dir():
        return jsonify({"error": "Path not found"}), 400

    items = []
    try:
        for child in sorted(p.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                items.append({"name": child.name, "path": str(child)})
    except PermissionError:
        pass

    parent = str(p.parent) if p.parent != p else ""
    return jsonify({"items": items, "current": str(p), "parent": parent})

# ─────────────────────────────────────────────────────────────────────────────
# .gitignore templates & management
# ─────────────────────────────────────────────────────────────────────────────

GITIGNORE_TEMPLATES = {
    "python": """# Python
__pycache__/
*.py[cod]
*.pyo
*.pyd
*.egg
*.egg-info/
dist/
build/
.eggs/
.venv/
venv/
env/
ENV/
*.virtualenv
pip-wheel-metadata/
.Python
site-packages/
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.cover
.coverage
htmlcov/
.tox/
""",
    "node": """# Node.js
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
pnpm-debug.log*
.pnpm-store/
.npm/
dist/
build/
.cache/
.parcel-cache/
.next/
.nuxt/
.output/
.vite/
*.tsbuildinfo
""",
    "java": """# Java / Maven / Gradle
target/
*.class
*.jar
*.war
*.ear
*.nar
.gradle/
build/
out/
.classpath
.project
.settings/
""",
    "dotnet": """# .NET / C#
bin/
obj/
*.user
*.suo
.vs/
packages/
*.nupkg
TestResults/
""",
    "rust": """# Rust
target/
Cargo.lock
*.pdb
""",
    "go": """# Go
vendor/
*.exe
*.test
*.out
go.sum
""",
    "general": """# OS & editor
.DS_Store
Thumbs.db
desktop.ini
.idea/
.vscode/
*.swp
*.swo
*~
.env
.env.local
.env.*.local
*.log
logs/
tmp/
temp/
""",
}

def detect_project_type(local_path: str) -> list[str]:
    """Sniff the project folder and return a list of detected types."""
    p = Path(local_path)
    detected = []
    markers = {
        "python":  ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile", "*.py"],
        "node":    ["package.json", "yarn.lock", "pnpm-lock.yaml"],
        "java":    ["pom.xml", "build.gradle", "gradlew", "*.java"],
        "dotnet":  ["*.csproj", "*.sln", "*.vbproj"],
        "rust":    ["Cargo.toml"],
        "go":      ["go.mod"],
    }
    for lang, files in markers.items():
        for pattern in files:
            if "*" in pattern:
                if any(p.glob(pattern)):
                    detected.append(lang)
                    break
            elif (p / pattern).exists():
                detected.append(lang)
                break
    return detected

def build_gitignore(types: list[str]) -> str:
    parts = [GITIGNORE_TEMPLATES["general"]]
    for t in types:
        if t in GITIGNORE_TEMPLATES:
            parts.append(f"# ── {t.capitalize()} ──\n" + GITIGNORE_TEMPLATES[t])
    return "\n".join(parts)

@app.route("/detect_project", methods=["POST"])
def detect_project():
    data = request.json
    path = data.get("path", "")
    if not path or not Path(path).exists():
        return jsonify({"types": [], "gitignore": GITIGNORE_TEMPLATES["general"]})
    types = detect_project_type(path)
    gitignore_path = Path(path) / ".gitignore"
    existing = gitignore_path.read_text() if gitignore_path.exists() else None
    suggested = build_gitignore(types)
    return jsonify({
        "types": types,
        "existing": existing,
        "suggested": suggested,
        "has_gitignore": gitignore_path.exists(),
    })

@app.route("/save_gitignore", methods=["POST"])
def save_gitignore():
    data = request.json
    path = data.get("path", "")
    content = data.get("content", "")
    if not path:
        return jsonify({"ok": False, "error": "No path"}), 400
    gitignore_path = Path(path) / ".gitignore"
    gitignore_path.write_text(content)
    return jsonify({"ok": True})

@app.route("/get_gitignore_template", methods=["POST"])
def get_gitignore_template():
    data = request.json
    t = data.get("type", "general")
    content = GITIGNORE_TEMPLATES.get(t, GITIGNORE_TEMPLATES["general"])
    return jsonify({"content": content})

@app.route("/get_gitignore", methods=["POST"])
def get_gitignore():
    data = request.json
    path = data.get("path", "")
    gitignore_path = Path(path) / ".gitignore"
    if gitignore_path.exists():
        return jsonify({"ok": True, "content": gitignore_path.read_text()})
    types = detect_project_type(path) if path and Path(path).exists() else []
    return jsonify({"ok": False, "suggested": build_gitignore(types), "types": types})

# ─────────────────────────────────────────────────────────────────────────────
# Routes — PULL (Drive → GitHub → Local)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/pull", methods=["POST"])
def pull():
    cfg = load_config()
    data = request.json
    commit_msg = data.get("commit_message", "sync: Drive → GitHub → Local").strip() or "sync: Drive → GitHub → Local"

    logs = []
    def log(msg): logs.append(msg)

    try:
        # ── Validate config ──────────────────────────────────────────────────
        if not cfg.get("local_path"):      return jsonify({"ok": False, "logs": ["❌ Local path not set."]})
        if not cfg.get("github_token"):    return jsonify({"ok": False, "logs": ["❌ GitHub token not set."]})
        if not cfg.get("github_repo"):     return jsonify({"ok": False, "logs": ["❌ GitHub repo not set."]})
        if not cfg.get("drive_folder_id"): return jsonify({"ok": False, "logs": ["❌ Drive folder not set."]})

        local_path = cfg["local_path"]
        Path(local_path).mkdir(parents=True, exist_ok=True)

        # ── Step 1: Download from Drive ──────────────────────────────────────
        log("📂 Connecting to Google Drive...")
        service = get_drive_service(cfg)
        if not service:
            return jsonify({"ok": False, "logs": ["❌ Google Drive not authenticated."]})

        log("🔍 Listing Drive folder contents...")
        files = list_drive_recursive(service, cfg["drive_folder_id"])
        log(f"   Found {len(files)} file(s) in Drive.")

        temp_dir = Path(local_path) / ".drivesync_temp"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir()

        for rel_path, file_id, mime_type in files:
            dest = temp_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            log(f"⬇️  {rel_path}")
            content = download_drive_file(service, file_id, mime_type)
            dest.write_bytes(content)

        log(f"✅ Downloaded {len(files)} file(s) from Drive.")

        # ── Step 2: Commit & push to GitHub ─────────────────────────────────
        log("\n🐙 Pushing to GitHub...")
        ensure_git_repo(str(temp_dir), cfg)

        git(str(temp_dir), "add", "-A")
        stdout, stderr, rc = git(str(temp_dir), "status", "--porcelain")
        if not stdout.strip():
            log("   ℹ️  No changes to commit.")
        else:
            _, err, rc = git(str(temp_dir), "commit", "-m", commit_msg)
            if rc != 0:
                log(f"   ⚠️  Commit warning: {err}")
            else:
                log(f"   ✅ Committed: {commit_msg}")

        # Try push; if branch doesn't exist upstream, set it up
        branch = cfg.get("github_branch", "main")
        out, err, rc = git(str(temp_dir), "push", "--set-upstream", "origin", branch)
        if rc != 0:
            # Branch may not exist; try force-push
            out, err, rc = git(str(temp_dir), "push", "-u", "origin", f"HEAD:{branch}")
        if rc != 0:
            log(f"   ❌ Push failed: {err}")
            shutil.rmtree(temp_dir)
            return jsonify({"ok": False, "logs": logs})
        log(f"   ✅ Pushed to github.com/{cfg['github_repo']} ({branch})")

        # ── Step 3: Pull from GitHub to local ────────────────────────────────
        log("\n💻 Syncing to local folder...")
        ensure_git_repo(local_path, cfg)

        # Fetch and reset to match remote
        git(local_path, "fetch", "origin")
        out, err, rc = git(local_path, "reset", "--hard", f"origin/{branch}")
        if rc != 0:
            # Branch may not exist locally yet — try checkout
            out, err, rc = git(local_path, "checkout", "-B", branch, f"origin/{branch}")
        if rc != 0:
            log(f"   ❌ Local pull failed: {err}")
        else:
            log(f"   ✅ Local folder updated.")

        # Cleanup temp
        shutil.rmtree(temp_dir)

        log("\n🎉 PULL complete: Drive → GitHub → Local")
        return jsonify({"ok": True, "logs": logs})

    except Exception as e:
        import traceback
        log(f"❌ Error: {e}")
        log(traceback.format_exc())
        return jsonify({"ok": False, "logs": logs})


# ─────────────────────────────────────────────────────────────────────────────
# Routes — PUSH (Local → GitHub → Drive)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/push", methods=["POST"])
def push():
    cfg = load_config()
    data = request.json
    commit_msg = data.get("commit_message", "sync: Local → GitHub → Drive").strip() or "sync: Local → GitHub → Drive"

    logs = []
    def log(msg): logs.append(msg)

    try:
        if not cfg.get("local_path"):      return jsonify({"ok": False, "logs": ["❌ Local path not set."]})
        if not cfg.get("github_token"):    return jsonify({"ok": False, "logs": ["❌ GitHub token not set."]})
        if not cfg.get("github_repo"):     return jsonify({"ok": False, "logs": ["❌ GitHub repo not set."]})
        if not cfg.get("drive_folder_id"): return jsonify({"ok": False, "logs": ["❌ Drive folder not set."]})

        local_path = cfg["local_path"]
        branch = cfg.get("github_branch", "main")

        if not Path(local_path).exists():
            return jsonify({"ok": False, "logs": [f"❌ Local path does not exist: {local_path}"]})

        # ── Step 1: Commit & push to GitHub ─────────────────────────────────
        log("🐙 Pushing local changes to GitHub...")
        ensure_git_repo(local_path, cfg)

        git(local_path, "add", "-A")
        stdout, _, _ = git(local_path, "status", "--porcelain")
        if not stdout.strip():
            log("   ℹ️  No local changes to commit.")
        else:
            _, err, rc = git(local_path, "commit", "-m", commit_msg)
            if rc != 0:
                log(f"   ⚠️  Commit warning: {err}")
            else:
                log(f"   ✅ Committed: {commit_msg}")

        out, err, rc = git(local_path, "push", "--set-upstream", "origin", branch)
        if rc != 0:
            out, err, rc = git(local_path, "push", "-u", "origin", f"HEAD:{branch}")
        if rc != 0:
            log(f"   ❌ Push to GitHub failed: {err}")
            return jsonify({"ok": False, "logs": logs})
        log(f"   ✅ Pushed to github.com/{cfg['github_repo']} ({branch})")

        # ── Step 2: Upload all files to Drive ───────────────────────────────
        log("\n📂 Uploading to Google Drive...")
        service = get_drive_service(cfg)
        if not service:
            return jsonify({"ok": False, "logs": logs + ["❌ Google Drive not authenticated."]})

        local_root = Path(local_path)
        # Use git ls-files so .gitignore is respected automatically
        out, err, rc = git(local_path, "ls-files", "--cached", "--others", "--exclude-standard")
        if rc == 0 and out.strip():
            all_files = [local_root / p for p in out.splitlines() if p.strip()]
            all_files = [f for f in all_files if f.is_file()]
            log(f"   Found {len(all_files)} file(s) to upload (respecting .gitignore).")
        else:
            # Fallback if git ls-files fails
            all_files = [
                f for f in local_root.rglob("*")
                if f.is_file() and ".git" not in f.parts and ".drivesync_temp" not in f.parts
            ]
            log(f"   Found {len(all_files)} file(s) to upload.")

        for f in all_files:
            rel = f.relative_to(local_root)
            log(f"⬆️  {rel}")
            upload_file_to_drive(service, f, cfg["drive_folder_id"], str(rel))

        log(f"\n🎉 PUSH complete: Local → GitHub → Drive")
        return jsonify({"ok": True, "logs": logs})

    except Exception as e:
        import traceback
        log(f"❌ Error: {e}")
        log(traceback.format_exc())
        return jsonify({"ok": False, "logs": logs})


# ─────────────────────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://localhost:5000")

if __name__ == "__main__":
    print("=" * 50)
    print("  DriveSync starting at http://localhost:5000")
    print("=" * 50)
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(debug=False, port=5000)
