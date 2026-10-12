"""Drive Declutter - local web tool to find and clean up Google Drive files.

Safety rules: files are only ever moved to Drive's trash (recoverable for 30 days),
only files you own are touched, and archived files are checksum-verified first.
"""
import hashlib
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import costs

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # localhost only
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

BASE = Path(__file__).parent
CREDENTIALS = BASE / "credentials.json"
TOKEN = BASE / "token.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]
REDIRECT = "http://localhost:5000/oauth2callback"

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24).hex())

CACHE = {"files": []}


def creds():
    if not TOKEN.exists():
        return None
    c = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if c.expired and c.refresh_token:
        c.refresh(Request())
        TOKEN.write_text(c.to_json())
    return c if c.valid else None


def drive():
    c = creds()
    return build("drive", "v3", credentials=c, cache_discovery=False) if c else None


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/login")
def login():
    if not CREDENTIALS.exists():
        return "Missing credentials.json - see README.", 500
    flow = Flow.from_client_secrets_file(str(CREDENTIALS), SCOPES, redirect_uri=REDIRECT)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["state"] = state
    session["verifier"] = flow.code_verifier
    return redirect(url)


@app.route("/oauth2callback")
def callback():
    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS), SCOPES, redirect_uri=REDIRECT, state=session.get("state")
    )
    flow.code_verifier = session.get("verifier")
    flow.fetch_token(authorization_response=request.url)
    TOKEN.write_text(flow.credentials.to_json())
    return redirect("/")


@app.route("/logout", methods=["POST"])
def logout():
    TOKEN.unlink(missing_ok=True)
    CACHE["files"] = []
    return jsonify(ok=True)


@app.route("/api/status")
def status():
    svc = drive()
    if not svc:
        return jsonify(signedIn=False)
    about = svc.about().get(fields="storageQuota,user(emailAddress)").execute()
    q = about["storageQuota"]
    return jsonify(
        signedIn=True,
        email=about["user"]["emailAddress"],
        usageBytes=int(q.get("usage", 0)),
        driveBytes=int(q.get("usageInDrive", 0)),
        trashBytes=int(q.get("usageInDriveTrash", 0)),
        limitBytes=int(q["limit"]) if q.get("limit") else None,
        scanned=len(CACHE["files"]),
    )


def slim(f):
    return {
        "id": f["id"],
        "name": f["name"],
        "mime": f["mimeType"],
        "size": int(f.get("size", 0)),
        "md5": f.get("md5Checksum"),
        "modified": f.get("modifiedTime"),
        "link": f.get("webViewLink"),
        "path": f.get("parents", [None])[0],
    }


@app.route("/api/scan", methods=["POST"])
def scan():
    svc = drive()
    if not svc:
        return jsonify(error="not signed in"), 401
    files, token = [], None
    while True:
        r = svc.files().list(
            q="trashed=false and 'me' in owners and mimeType!='application/vnd.google-apps.folder'",
            pageSize=1000,
            pageToken=token,
            fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime,webViewLink,parents)",
        ).execute()
        files += [slim(f) for f in r.get("files", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    CACHE["files"] = files
    return jsonify(count=len(files))


@app.route("/api/report")
def report():
    files = CACHE["files"]
    sized = [f for f in files if f["size"] > 0]
    years = int(request.args.get("years", 2))
    min_mb = float(request.args.get("minMb", 50))
    cutoff = datetime.now(timezone.utc) - timedelta(days=365 * years)

    large = sorted((f for f in sized if f["size"] >= min_mb * 1024 ** 2), key=lambda f: -f["size"])[:300]

    groups = defaultdict(list)
    for f in sized:
        if f["md5"]:
            groups[f["md5"]].append(f)
    dupes = []
    for md5, items in groups.items():
        if len(items) > 1:
            items.sort(key=lambda f: f["modified"] or "")
            dupes.append({"md5": md5, "wasted": items[0]["size"] * (len(items) - 1), "files": items})
    dupes.sort(key=lambda g: -g["wasted"])

    def is_old(f):
        return f["modified"] and datetime.fromisoformat(f["modified"].replace("Z", "+00:00")) < cutoff

    old = sorted((f for f in sized if is_old(f)), key=lambda f: -f["size"])[:300]
    return jsonify(large=large, duplicates=dupes[:200], old=old,
                   wastedByDupes=sum(g["wasted"] for g in dupes))


@app.route("/api/advice")
def advice():
    used = int(request.args.get("used", 0))
    archive = int(request.args.get("archive", 0))
    return jsonify(costs.advise(used, archive))


@app.route("/api/trash", methods=["POST"])
def trash():
    svc = drive()
    ids = request.json.get("ids", [])
    done, failed = [], []
    for fid in ids:
        try:
            svc.files().update(fileId=fid, body={"trashed": True}).execute()
            done.append(fid)
        except Exception as e:
            failed.append({"id": fid, "error": str(e)})
    CACHE["files"] = [f for f in CACHE["files"] if f["id"] not in done]
    return jsonify(trashed=done, failed=failed)


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name)


@app.route("/api/archive", methods=["POST"])
def archive():
    """Download files to a local folder (e.g. an external disk), verify MD5, optionally trash."""
    svc = drive()
    body = request.json
    dest = Path(body["dest"])
    if not dest.parent.exists():
        return jsonify(error=f"Folder location {dest.parent} does not exist"), 400
    dest.mkdir(exist_ok=True)
    by_id = {f["id"]: f for f in CACHE["files"]}
    results = []
    for fid in body.get("ids", []):
        f = by_id.get(fid)
        if not f or not f["md5"]:
            results.append({"id": fid, "status": "skipped (no checksum)"})
            continue
        target = dest / f"{f['id'][:6]}_{safe_name(f['name'])}"
        h = hashlib.md5()
        with open(target, "wb") as out:
            dl = MediaIoBaseDownload(out, svc.files().get_media(fileId=fid), chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != f["md5"]:
            target.unlink(missing_ok=True)
            results.append({"id": fid, "status": "checksum mismatch - not trashed"})
            continue
        if body.get("trashAfter"):
            svc.files().update(fileId=fid, body={"trashed": True}).execute()
            CACHE["files"] = [x for x in CACHE["files"] if x["id"] != fid]
        results.append({"id": fid, "status": "archived", "path": str(target)})
    return jsonify(results=results)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
