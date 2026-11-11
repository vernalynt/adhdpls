"""Immich connection: pull phone/computer screenshots in as image cards.

Config (environment, never hard-coded):
  IMMICH_URL       base URL, e.g. http://host.docker.internal:2283
  IMMICH_API_KEY   key with asset.read and asset.view permissions
  IMMICH_NAME_PREFIX  filename prefix that marks a screenshot (default "Screenshot")

Written against the Immich v3.2.x OpenAPI spec: POST /api/search/metadata with
the new `filter` / `cursor` / `orderBy` fields, GET /api/assets/{id}/thumbnail.
"""
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime

URL = os.environ.get("IMMICH_URL", "").rstrip("/")
KEY = os.environ.get("IMMICH_API_KEY", "")
PREFIX = os.environ.get("IMMICH_NAME_PREFIX", "Screenshot")
TIMEOUT = 20


def configured():
    return bool(URL and KEY)


def _req(path, body=None):
    req = urllib.request.Request(
        URL + "/api" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-api-key": KEY, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if body is not None else "GET",
    )
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def fetch_image(asset_id, size="thumbnail"):
    """Return (bytes, content_type) for an asset image. size: thumbnail|preview|fullsize."""
    if size not in ("thumbnail", "preview", "fullsize"):
        size = "thumbnail"
    with _req(f"/assets/{asset_id}/thumbnail?size={size}") as r:
        return r.read(), r.headers.get("Content-Type", "image/webp")


def _search_page(cursor=None, size=100):
    body = {
        "filter": {
            "originalFileName": {"startsWith": PREFIX},
            "type": {"eq": "IMAGE"},
        },
        "orderBy": {"field": "fileCreatedAt", "direction": "desc"},
        "size": size,
    }
    if cursor:
        body["cursor"] = cursor
    with _req("/search/metadata", body) as r:
        data = json.load(r)["assets"]
    return data["items"], data.get("nextCursor")


def sync(db_path, max_new=300):
    """Import screenshots not yet in the database. Returns a summary dict.

    Walks every page of matching screenshots, newest first, and skips the ones
    already imported or dismissed. It does NOT stop at the first known asset:
    the phone uploads in whatever order it likes, so older screenshots can
    arrive after newer ones. Each run imports at most max_new; run again to
    continue a big backlog.
    """
    if not configured():
        return {"ok": False, "error": "Immich is not configured (IMMICH_URL / IMMICH_API_KEY)."}
    conn = sqlite3.connect(db_path)
    try:
        known = {r[0] for r in conn.execute("SELECT immich_id FROM cards WHERE immich_id IS NOT NULL")}
        known |= {r[0] for r in conn.execute("SELECT immich_id FROM dismissed")}
        fresh, cursor, scanned, more = [], None, 0, False
        while True:
            items, cursor = _search_page(cursor)
            scanned += len(items)
            for a in items:
                if a["id"] not in known:
                    fresh.append(a)
                    if len(fresh) >= max_new:
                        break
            if len(fresh) >= max_new:
                more = bool(cursor) or len(items) > 0
                break
            if not cursor or not items:
                break
        # Insert oldest first so the newest screenshot gets the highest card id.
        for a in reversed(fresh):
            created = (a.get("fileCreatedAt") or datetime.now().isoformat())[:19]
            conn.execute(
                "INSERT INTO cards (type, text, items, tags, image, immich_id, created_at) "
                "VALUES ('note', '', '[]', 'screenshot', ?, ?, ?)",
                (f"/immich/img/{a['id']}?size=preview", a["id"], created),
            )
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES ('screenshot')")
        conn.commit()
        return {"ok": True, "imported": len(fresh), "more": more, "scanned": scanned}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Immich returned HTTP {e.code}. Check the API key permissions."}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "error": f"Could not reach Immich: {e}"}
    finally:
        conn.close()
