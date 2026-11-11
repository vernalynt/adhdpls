"""ADHDpls: a personal, self-hosted dashboard for ADHD brains. Notes, lists, reminders, tags, search.

Run locally:  python app.py   (http://127.0.0.1:5055)
Data lives in DATA_DIR/adhdpls.db (SQLite).
"""
import json
import os
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

import threading
import time
import uuid

from flask import Flask, Response, g, jsonify, make_response, request, send_from_directory

import gcal
import immich

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "adhdpls.db")
os.makedirs(DATA_DIR, exist_ok=True)

UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB per request

UPLOAD_NAME_RE = re.compile(r"[0-9a-f]{32}\.(png|jpg|gif|webp)")
# Only these image values are accepted on a card (no arbitrary external URLs).
IMAGE_RE = re.compile(r"/uploads/[0-9a-f]{32}\.(png|jpg|gif|webp)|/immich/img/[0-9a-fA-F-]{36}\?size=\w+")


def sniff_image(head):
    """Identify the image type from its first bytes, not from the filename."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"GIF8"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'note',      -- note | list | reminder
    text TEXT NOT NULL DEFAULT '',
    items TEXT NOT NULL DEFAULT '[]',       -- JSON [{"t": "...", "done": false}]
    tags TEXT NOT NULL DEFAULT '',          -- comma separated, lowercase, no #
    remind_at TEXT,                         -- ISO local time, reminders only
    reminded INTEGER NOT NULL DEFAULT 0,    -- reserved for future notifications
    done INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    image TEXT,
    immich_id TEXT,                         -- Immich asset id for screenshot cards
    gcal_event_id TEXT,                     -- Google Calendar event for reminder cards
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS dismissed (   -- screenshots you deleted: never re-import
    immich_id TEXT PRIMARY KEY
);
"""

TAG_RE = re.compile(r"#([A-Za-z0-9_]+)")
BUILTIN_TAGS = ("list", "reminder")  # these two decide how a card is drawn


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cards)")]
    if "immich_id" not in cols:  # database made before the Immich feature
        conn.execute("ALTER TABLE cards ADD COLUMN immich_id TEXT")
    if "gcal_event_id" not in cols:  # database made before the Calendar feature
        conn.execute("ALTER TABLE cards ADD COLUMN gcal_event_id TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_immich ON cards(immich_id) WHERE immich_id IS NOT NULL")
    for t in BUILTIN_TAGS:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))
    # Older cards stored the type separately: give them the matching hashtag.
    for ctype in BUILTIN_TAGS:
        for cid, tags in conn.execute("SELECT id, tags FROM cards WHERE type=?", (ctype,)).fetchall():
            if ctype not in tags.split(","):
                conn.execute("UPDATE cards SET tags=? WHERE id=?", (",".join([t for t in tags.split(",") if t] + [ctype]), cid))
    conn.commit()
    conn.close()


def row_to_card(r):
    return {
        "id": r["id"],
        "type": r["type"],
        "text": r["text"],
        "items": json.loads(r["items"] or "[]"),
        "tags": [t for t in r["tags"].split(",") if t],
        "remind_at": r["remind_at"],
        "done": bool(r["done"]),
        "url": r["url"],
        "image": r["image"],
        "on_calendar": bool(r["gcal_event_id"]),
        "created_at": r["created_at"],
    }


def gcal_push(conn, row):
    """Create or update the calendar event for a reminder card. Never raises:
    a Google outage must not stop you saving a reminder (the background loop retries)."""
    if not gcal.configured() or row["type"] != "reminder" or not row["remind_at"] or row["done"]:
        return
    try:
        eid = gcal.upsert(row["gcal_event_id"], row["text"], row["remind_at"])
        conn.execute("UPDATE cards SET gcal_event_id=? WHERE id=?", (eid, row["id"]))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("Calendar sync failed for card %s: %s", row["id"], exc)


def clean_tags(text, tags):
    found = [t.lower() for t in TAG_RE.findall(text or "")]
    given = [t.lower().lstrip("#").strip() for t in (tags or [])]
    out = []
    for t in found + given:
        if t and t not in out:
            out.append(t)
    return ",".join(out)


@app.before_request
def same_origin_only():
    """Refuse cross-site writes, so a web page open in your browser cannot edit your dashboard."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return jsonify({"error": "Cross-site request refused."}), 403
    return None


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/cards")
def list_cards():
    q = request.args.get("q", "").strip().lower()
    tag = request.args.get("tag", "").strip().lower()
    ctype = request.args.get("type", "").strip().lower()
    rows = db().execute("SELECT * FROM cards ORDER BY id DESC").fetchall()
    cards = [row_to_card(r) for r in rows]
    if request.args.get("unsorted"):
        # "Needs sorting": image cards with no tag, or only the automatic #screenshot tag
        cards = [c for c in cards if c["image"] and c["tags"] in ([], ["screenshot"])]
    if ctype:
        cards = [c for c in cards if c["type"] == ctype]
    if tag:
        cards = [c for c in cards if tag in c["tags"]]
    if q:
        def hit(c):
            blob = c["text"].lower() + " " + " ".join(i["t"].lower() for i in c["items"])
            return q in blob or any(q in t for t in c["tags"])
        cards = [c for c in cards if hit(c)]
    return jsonify(cards)


def register_tags(csv):
    for t in csv.split(","):
        if t:
            db().execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (t,))


@app.get("/api/tags")
def list_tags():
    """All categories with card counts. Includes empty categories."""
    counts = {r["name"]: 0 for r in db().execute("SELECT name FROM tags")}
    for r in db().execute("SELECT tags FROM cards"):
        for t in r["tags"].split(","):
            if t:
                counts[t] = counts.get(t, 0) + 1
    return jsonify(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


@app.post("/api/tags")
def create_tag():
    name = re.sub(r"[^a-z0-9_]", "", (request.get_json(force=True).get("name") or "").lower().lstrip("#"))
    if not name:
        return jsonify({"error": "bad name"}), 400
    db().execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    db().commit()
    return jsonify({"name": name}), 201


@app.delete("/api/tags/<name>")
def delete_tag(name):
    """Remove a category and strip it from every card (cards are kept)."""
    name = name.lower()
    if name in BUILTIN_TAGS:
        return jsonify({"error": "built-in tag"}), 400
    db().execute("DELETE FROM tags WHERE name=?", (name,))
    for r in db().execute("SELECT id, tags FROM cards").fetchall():
        kept = [t for t in r["tags"].split(",") if t and t != name]
        db().execute("UPDATE cards SET tags=? WHERE id=?", (",".join(kept), r["id"]))
    db().commit()
    return "", 204


@app.post("/api/cards")
def create_card():
    d = request.get_json(force=True)
    text = (d.get("text") or "").strip()
    items = [{"t": str(i).strip(), "done": False} for i in d.get("items", []) if str(i).strip()]
    image = d.get("image") if IMAGE_RE.fullmatch(d.get("image") or "") else None
    if not text and not items and not image:
        return jsonify({"error": "empty card"}), 400
    # Type is a hashtag: #list and #reminder are built in. An explicit "type"
    # field (for API clients) just adds the matching tag.
    given = list(d.get("tags") or [])
    if d.get("type") in BUILTIN_TAGS:
        given.append(d["type"])
    tag_csv = clean_tags(text, given)
    tagset = tag_csv.split(",")
    ctype = "list" if "list" in tagset else "reminder" if "reminder" in tagset else "note"
    register_tags(tag_csv)
    cur = db().execute(
        "INSERT INTO cards (type, text, items, tags, remind_at, url, image, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ctype,
            text,
            json.dumps(items),
            tag_csv,
            d.get("remind_at") or None,
            d.get("url") or None,
            image,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db().commit()
    row = db().execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone()
    gcal_push(db(), row)
    row = db().execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(row_to_card(row)), 201


@app.patch("/api/cards/<int:cid>")
def update_card(cid):
    d = request.get_json(force=True)
    row = db().execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    text = d.get("text", row["text"])
    items = json.dumps(d["items"]) if "items" in d else row["items"]
    tags = clean_tags(text, d["tags"]) if "tags" in d else row["tags"]
    db().execute(
        "UPDATE cards SET text=?, items=?, tags=?, remind_at=?, done=? WHERE id=?",
        (
            text,
            items,
            tags,
            d.get("remind_at", row["remind_at"]),
            int(d["done"]) if "done" in d else row["done"],
            cid,
        ),
    )
    db().commit()
    row = db().execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    gcal_push(db(), row)
    row = db().execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    return jsonify(row_to_card(row))


@app.delete("/api/cards/<int:cid>")
def delete_card(cid):
    row = db().execute("SELECT image, gcal_event_id FROM cards WHERE id=?", (cid,)).fetchone()
    if row and row["gcal_event_id"] and gcal.configured():
        try:
            gcal.delete(row["gcal_event_id"])
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("Could not delete calendar event for card %s: %s", cid, exc)
    if row and row["image"] and row["image"].startswith("/uploads/"):
        name = row["image"].rsplit("/", 1)[-1]
        if UPLOAD_NAME_RE.fullmatch(name):
            try:
                os.remove(os.path.join(UPLOAD_DIR, name))
            except OSError:
                pass
    db().execute(
        "INSERT OR IGNORE INTO dismissed (immich_id) SELECT immich_id FROM cards WHERE id=? AND immich_id IS NOT NULL",
        (cid,),
    )
    db().execute("DELETE FROM cards WHERE id=?", (cid,))
    db().commit()
    return "", 204


@app.post("/api/upload")
def upload_image():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    head = f.stream.read(12)
    f.stream.seek(0)
    ext = sniff_image(head)
    if not ext:
        return jsonify({"error": "Only PNG, JPEG, GIF or WebP images are supported."}), 415
    name = f"{uuid.uuid4().hex}.{ext}"
    f.save(os.path.join(UPLOAD_DIR, name))
    return jsonify({"url": f"/uploads/{name}"}), 201


@app.get("/uploads/<name>")
def serve_upload(name):
    if not UPLOAD_NAME_RE.fullmatch(name):
        return "not found", 404
    mime = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
    resp = make_response(send_from_directory(UPLOAD_DIR, name, mimetype=mime, max_age=86400))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/api/immich/status")
def immich_status():
    return jsonify({"configured": immich.configured()})


@app.post("/api/immich/sync")
def immich_sync():
    result = immich.sync(DB_PATH)
    return jsonify(result), (200 if result["ok"] else 502)


@app.get("/immich/img/<asset_id>")
def immich_img(asset_id):
    """Proxy images so the API key never reaches the browser."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", asset_id):
        return "bad id", 400
    try:
        data, ctype = immich.fetch_image(asset_id, request.args.get("size", "thumbnail"))
    except Exception:
        return "image unavailable", 502
    return Response(data, mimetype=ctype, headers={"Cache-Control": "private, max-age=86400"})


def _auto_sync_loop(every=300):
    time.sleep(10)
    while True:
        try:
            if immich.configured():
                immich.sync(DB_PATH)
        except Exception:
            pass  # never let the sync thread die
        try:
            retry_calendar()
        except Exception:
            pass
        time.sleep(every)


def retry_calendar():
    """Push any reminder that is not on the calendar yet (Google was down, or the
    reminder was made before Calendar was set up)."""
    if not gcal.configured():
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
            "SELECT * FROM cards WHERE type='reminder' AND remind_at IS NOT NULL "
            "AND gcal_event_id IS NULL AND done=0"
        ).fetchall():
            gcal_push(conn, row)
    finally:
        conn.close()


def start_background():
    if immich.configured() or gcal.configured():
        threading.Thread(target=_auto_sync_loop, daemon=True).start()


if __name__ == "__main__":
    init_db()
    start_background()
    # HOST is 127.0.0.1 locally; in Docker it is 0.0.0.0 and the compose file
    # binds the published port to the address you choose (BIND_IP).
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=5055)
else:
    init_db()
    start_background()
