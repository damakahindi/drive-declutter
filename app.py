"""Drive Declutter - local web tool to find and clean up Google Drive files.

Safety rules: files are only ever moved to Drive's trash (recoverable for 30 days),
only files you own are touched, and archived files are checksum-verified first.
"""
import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory, session
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import costs
import rules as R

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # localhost only
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

BASE = Path(__file__).parent
CREDENTIALS = BASE / "credentials.json"
TOKEN = BASE / "token.json"
IGNORED = BASE / "ignored.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]
REDIRECT = "http://localhost:5000/oauth2callback"
REVIEW_FOLDER = "Drive Declutter - Review"

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


def ignored_ids():
    return set(json.loads(IGNORED.read_text())) if IGNORED.exists() else set()


def drop(ids):
    ids = set(ids)
    CACHE["files"] = [f for f in CACHE["files"] if f["id"] not in ids]


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
    has_creds = CREDENTIALS.exists()
    svc = drive()
    if not svc:
        return jsonify(signedIn=False, credentialsPresent=has_creds)
    about = svc.about().get(fields="storageQuota,user(displayName,emailAddress,photoLink)").execute()
    q, u = about["storageQuota"], about["user"]
    return jsonify(
        signedIn=True, credentialsPresent=has_creds,
        email=u["emailAddress"], name=u.get("displayName"), photo=u.get("photoLink"),
        usageBytes=int(q.get("usage", 0)),
        driveBytes=int(q.get("usageInDrive", 0)),
        trashBytes=int(q.get("usageInDriveTrash", 0)),
        limitBytes=int(q["limit"]) if q.get("limit") else None,
        scanned=len(CACHE["files"]),
    )


def slim(f):
    return {
        "id": f["id"], "name": f["name"], "mime": f["mimeType"],
        "size": int(f.get("size", 0)), "md5": f.get("md5Checksum"),
        "modified": f.get("modifiedTime"), "created": f.get("createdTime"),
        "link": f.get("webViewLink"), "thumb": f.get("thumbnailLink"),
        "ext": f.get("fileExtension"), "starred": bool(f.get("starred")),
        "shared": bool(f.get("shared")),
    }


@app.route("/api/scan", methods=["POST"])
def scan():
    svc = drive()
    if not svc:
        return jsonify(error="not signed in"), 401
    files, token = [], None
    fields = ("nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime,createdTime,"
              "webViewLink,thumbnailLink,fileExtension,starred,shared)")
    while True:
        r = svc.files().list(
            q="trashed=false and 'me' in owners and mimeType!='application/vnd.google-apps.folder'",
            pageSize=1000, pageToken=token, fields=fields,
        ).execute()
        files += [slim(f) for f in r.get("files", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    CACHE["files"] = files
    return jsonify(count=len(files))


def sized():
    ig = ignored_ids()
    return [f for f in CACHE["files"] if f["size"] > 0 and f["id"] not in ig]


@app.route("/api/overview")
def overview():
    files = sized()
    by_cat = defaultdict(lambda: {"count": 0, "bytes": 0})
    for f in files:
        c = by_cat[R.category(f)]
        c["count"] += 1
        c["bytes"] += f["size"]
    extras = R.duplicate_extras(files)
    wasted = sum(f["size"] for f in files if f["id"] in extras)
    return jsonify(categories=by_cat, wastedByDupes=wasted, duplicateCount=len(extras))


@app.route("/api/suggestions")
def suggestions():
    return jsonify(buckets=R.suggest(CACHE["files"], R.load_rules(), ignored_ids()))


@app.route("/api/browse")
def browse():
    cat = request.args.get("category", "image")
    min_mb = float(request.args.get("minMb", 0))
    items = [f for f in sized() if R.category(f) == cat and f["size"] >= min_mb * 1024 ** 2]
    items.sort(key=lambda f: -f["size"])
    return jsonify(files=items[:300], total=len(items))


@app.route("/api/duplicates")
def duplicates():
    groups = defaultdict(list)
    for f in sized():
        if f["md5"]:
            groups[f["md5"]].append(f)
    out = []
    for md5, items in groups.items():
        if len(items) > 1:
            items.sort(key=lambda f: f["created"] or "")
            out.append({"md5": md5, "wasted": items[0]["size"] * (len(items) - 1), "files": items})
    out.sort(key=lambda g: -g["wasted"])
    return jsonify(groups=out[:200])


@app.route("/api/rules", methods=["GET", "PUT"])
def rules_api():
    if request.method == "PUT":
        data = request.json
        for r in data:
            if r.get("action") not in R.ACTIONS or "name" not in r or "match" not in r:
                return jsonify(error=f"Invalid rule: {r.get('name')}"), 400
            if "nameRegex" in r["match"]:
                try:
                    re.compile(r["match"]["nameRegex"])
                except re.error as e:
                    return jsonify(error=f"Bad regex in '{r['name']}': {e}"), 400
        R.save_rules(data)
    return jsonify(rules=R.load_rules(), actions=R.ACTIONS, defaults=R.DEFAULT_RULES)


@app.route("/api/advice")
def advice():
    return jsonify(costs.advise(int(request.args.get("used", 0)), int(request.args.get("archive", 0))))


@app.route("/api/ignore", methods=["POST"])
def ignore():
    ids = ignored_ids() | set(request.json.get("ids", []))
    IGNORED.write_text(json.dumps(sorted(ids)))
    return jsonify(ignored=len(ids))


@app.route("/api/trash", methods=["POST"])
def trash():
    svc = drive()
    done, failed = [], []
    for fid in request.json.get("ids", []):
        try:
            svc.files().update(fileId=fid, body={"trashed": True}).execute()
            done.append(fid)
        except Exception as e:
            failed.append({"id": fid, "error": str(e)})
    drop(done)
    return jsonify(trashed=done, failed=failed)


def review_folder(svc):
    q = f"name='{REVIEW_FOLDER}' and mimeType='application/vnd.google-apps.folder' and trashed=false and 'me' in owners"
    found = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    if found:
        return found[0]["id"]
    meta = {"name": REVIEW_FOLDER, "mimeType": "application/vnd.google-apps.folder"}
    return svc.files().create(body=meta, fields="id").execute()["id"]


@app.route("/api/move", methods=["POST"])
def move():
    """Move files into one review folder so you can decide later; frees nothing but declutters."""
    svc = drive()
    folder = review_folder(svc)
    done, failed = [], []
    for fid in request.json.get("ids", []):
        try:
            parents = svc.files().get(fileId=fid, fields="parents").execute().get("parents", [])
            svc.files().update(fileId=fid, addParents=folder, removeParents=",".join(parents),
                               fields="id").execute()
            done.append(fid)
        except Exception as e:
            failed.append({"id": fid, "error": str(e)})
    drop(done)
    return jsonify(moved=done, failed=failed)


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
        with open(target, "wb") as out:
            dl = MediaIoBaseDownload(out, svc.files().get_media(fileId=fid), chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
        h = hashlib.md5()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != f["md5"]:
            target.unlink(missing_ok=True)
            results.append({"id": fid, "status": "checksum mismatch - not trashed"})
            continue
        if body.get("trashAfter"):
            svc.files().update(fileId=fid, body={"trashed": True}).execute()
            drop([fid])
        results.append({"id": fid, "status": "archived", "path": str(target)})
    return jsonify(results=results)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
