"""Rule engine: classifies files and turns rules into cleanup suggestions."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

RULES_FILE = Path(__file__).parent / "rules.json"

ACTIONS = {
    "archive_trash": "Archive to disk, then trash",
    "archive": "Archive to disk (keep in Drive)",
    "move_review": "Move to 'Drive Declutter - Review' folder",
    "trash": "Move to trash",
    "keep": "Keep (never suggest)",
}

# First matching rule wins per file, so order matters: put protective "keep" rules first.
DEFAULT_RULES = [
    {"name": "Never touch starred or shared files", "enabled": True, "action": "keep",
     "match": {"starredOrShared": True}},
    {"name": "Exact duplicates (extra copies)", "enabled": True, "action": "trash",
     "match": {"duplicate": True}},
    {"name": "Screenshots older than 90 days", "enabled": True, "action": "trash",
     "match": {"nameRegex": "^(screenshot|screen shot|capture)", "category": "image", "olderThanDays": 90}},
    {"name": "Large photos over 10 MB", "enabled": True, "action": "archive_trash",
     "match": {"category": "image", "minMb": 10}},
    {"name": "Videos over 200 MB older than 1 year", "enabled": True, "action": "archive_trash",
     "match": {"category": "video", "minMb": 200, "olderThanDays": 365}},
    {"name": "WhatsApp media older than 6 months", "enabled": True, "action": "archive_trash",
     "match": {"nameRegex": "^(IMG|VID|PTT|AUD)-\\d{8}-WA", "olderThanDays": 180}},
    {"name": "Installers and disk images (exe, msi, dmg, iso, apk)", "enabled": True, "action": "trash",
     "match": {"extensions": ["exe", "msi", "dmg", "iso", "apk"]}},
    {"name": "Zip/archive files over 100 MB older than 1 year", "enabled": True, "action": "archive_trash",
     "match": {"category": "archive", "minMb": 100, "olderThanDays": 365}},
    {"name": "Any file over 500 MB (review first)", "enabled": True, "action": "move_review",
     "match": {"minMb": 500}},
    {"name": "Untouched for 3+ years and over 20 MB", "enabled": False, "action": "move_review",
     "match": {"olderThanDays": 1095, "minMb": 20}},
]

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "heic", "heif", "webp", "bmp", "tif", "tiff", "raw", "cr2", "nef", "dng"}
VIDEO_EXT = {"mp4", "mov", "avi", "mkv", "wmv", "webm", "m4v", "3gp"}
AUDIO_EXT = {"mp3", "wav", "m4a", "flac", "aac", "ogg"}
ARCHIVE_EXT = {"zip", "rar", "7z", "tar", "gz", "bz2"}
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv"}


def category(f):
    mime = f.get("mime", "")
    ext = (f.get("ext") or "").lower()
    if mime.startswith("image/") or ext in IMAGE_EXT:
        return "image"
    if mime.startswith("video/") or ext in VIDEO_EXT:
        return "video"
    if mime.startswith("audio/") or ext in AUDIO_EXT:
        return "audio"
    if ext in ARCHIVE_EXT or "zip" in mime:
        return "archive"
    if ext in DOC_EXT or mime.startswith("application/pdf"):
        return "document"
    return "other"


def load_rules():
    if RULES_FILE.exists():
        return json.loads(RULES_FILE.read_text())
    return DEFAULT_RULES


def save_rules(rules):
    RULES_FILE.write_text(json.dumps(rules, indent=2))


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def duplicate_extras(files):
    """IDs of redundant copies; the oldest copy in each checksum group is kept."""
    groups = {}
    for f in files:
        if f["md5"] and f["size"] > 0:
            groups.setdefault(f["md5"], []).append(f)
    extras = set()
    for items in groups.values():
        if len(items) > 1:
            items.sort(key=lambda f: f["created"] or "")
            extras.update(f["id"] for f in items[1:])
    return extras


def matches(f, m, extras, now):
    if m.get("starredOrShared") and not (f["starred"] or f["shared"]):
        return False
    if m.get("duplicate") and f["id"] not in extras:
        return False
    if "category" in m and category(f) != m["category"]:
        return False
    if "minMb" in m and f["size"] < m["minMb"] * 1024 ** 2:
        return False
    if "maxMb" in m and f["size"] > m["maxMb"] * 1024 ** 2:
        return False
    if "olderThanDays" in m:
        t = parse_time(f["modified"])
        if not t or t > now - timedelta(days=m["olderThanDays"]):
            return False
    if "extensions" in m and (f.get("ext") or "").lower() not in [e.lower() for e in m["extensions"]]:
        return False
    if "nameRegex" in m and not re.search(m["nameRegex"], f["name"], re.IGNORECASE):
        return False
    return True


def suggest(files, rules, ignored):
    now = datetime.now(timezone.utc)
    extras = duplicate_extras(files)
    claimed, buckets = set(), []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        hit = [f for f in files
               if f["id"] not in claimed and f["id"] not in ignored and f["size"] > 0
               and matches(f, rule["match"], extras, now)]
        claimed.update(f["id"] for f in hit)
        if rule["action"] == "keep" or not hit:
            continue
        hit.sort(key=lambda f: -f["size"])
        buckets.append({
            "rule": rule["name"], "action": rule["action"],
            "actionLabel": ACTIONS.get(rule["action"], rule["action"]),
            "count": len(hit), "bytes": sum(f["size"] for f in hit), "files": hit[:200],
        })
    return buckets
