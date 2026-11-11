"""Tests for ADHDpls.

Run from the project folder:
    pip install -r requirements.txt
    python -m unittest discover -s tests -v

Each test gets its own empty database. Immich and Google Calendar are replaced by fakes,
so no network is used.
"""
import io
import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="adhdpls-test-")
os.environ["DATA_DIR"] = _TMP
for var in ("IMMICH_URL", "IMMICH_API_KEY", "GCAL_CALENDAR_ID"):
    os.environ.pop(var, None)
import app as A  # noqa: E402  (must come after the environment is set)
import gcal  # noqa: E402
import immich  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(dir=_TMP)
        A.DB_PATH = os.path.join(d, "adhdpls.db")
        A.UPLOAD_DIR = os.path.join(d, "uploads")
        os.makedirs(A.UPLOAD_DIR)
        A.init_db()
        self.c = A.app.test_client()

    def card(self, **body):
        r = self.c.post("/api/cards", json=body)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def cards(self, query=""):
        return self.c.get("/api/cards" + query).get_json()

    def upload(self, data=PNG, name="x.png"):
        return self.c.post("/api/upload", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


class Cards(Base):
    def test_type_comes_from_hashtags(self):
        self.assertEqual(self.card(text="plain thought")["type"], "note")
        lst = self.card(text="Errands #list", items=["milk", "stamps"])
        self.assertEqual(lst["type"], "list")
        self.assertEqual([i["t"] for i in lst["items"]], ["milk", "stamps"])
        rem = self.card(text="Renew passport", tags=["reminder"], remind_at="2030-01-01T09:00")
        self.assertEqual(rem["type"], "reminder")

    def test_hashtags_typed_in_text_become_tags(self):
        c = self.card(text="Idea for the garden #Ideas #home")
        self.assertEqual(c["tags"], ["ideas", "home"])

    def test_empty_card_is_refused(self):
        self.assertEqual(self.c.post("/api/cards", json={"text": "  "}).status_code, 400)

    def test_search_checks_text_list_items_and_tags(self):
        self.card(text="call the dentist #health")
        self.card(text="Shopping #list", items=["oat milk"])
        self.assertEqual(len(self.cards("?q=dentist")), 1)
        self.assertEqual(len(self.cards("?q=oat")), 1)
        self.assertEqual(len(self.cards("?q=health")), 1)
        self.assertEqual(len(self.cards("?q=nothing-here")), 0)

    def test_update_marks_items_done(self):
        c = self.card(text="Todo #list", items=["a", "b"])
        items = [{"t": "a", "done": True}, {"t": "b", "done": False}]
        r = self.c.patch(f"/api/cards/{c['id']}", json={"items": items}).get_json()
        self.assertEqual([i["done"] for i in r["items"]], [True, False])
        self.assertEqual(self.c.patch("/api/cards/9999", json={"text": "x"}).status_code, 404)


class Tags(Base):
    def test_built_in_tags_cannot_be_deleted(self):
        self.assertEqual(self.c.delete("/api/tags/list").status_code, 400)
        self.assertEqual(self.c.delete("/api/tags/reminder").status_code, 400)

    def test_deleting_a_tag_keeps_the_cards(self):
        self.c.post("/api/tags", json={"name": "#Ideas"})
        card = self.card(text="a thought", tags=["ideas"])
        self.assertEqual(self.c.delete("/api/tags/ideas").status_code, 204)
        after = self.cards()
        self.assertEqual([c["id"] for c in after], [card["id"]])
        self.assertEqual(after[0]["tags"], [])

    def test_tag_counts_include_empty_categories(self):
        self.c.post("/api/tags", json={"name": "empty"})
        self.card(text="x #used")
        counts = dict(self.c.get("/api/tags").get_json())
        self.assertEqual(counts["empty"], 0)
        self.assertEqual(counts["used"], 1)


class Images(Base):
    def test_only_real_images_are_accepted(self):
        self.assertEqual(self.upload(b"just text, not an image", "evil.png").status_code, 415)
        r = self.upload()
        self.assertEqual(r.status_code, 201)
        url = r.get_json()["url"]
        got = self.c.get(url)
        self.assertEqual(got.content_type, "image/png")
        self.assertEqual(got.headers["X-Content-Type-Options"], "nosniff")
        got.close()

    def test_external_image_urls_are_not_stored(self):
        r = self.c.post("/api/cards", json={"image": "http://evil.example/x.png"})
        self.assertEqual(r.status_code, 400)  # nothing else on the card, so it is empty

    def test_deleting_a_card_removes_its_uploaded_file(self):
        url = self.upload().get_json()["url"]
        card = self.card(image=url)
        path = os.path.join(A.UPLOAD_DIR, url.rsplit("/", 1)[-1])
        self.assertTrue(os.path.exists(path))
        self.c.delete(f"/api/cards/{card['id']}")
        self.assertFalse(os.path.exists(path))

    def test_needs_sorting_lists_untagged_images_only(self):
        url = self.upload().get_json()["url"]
        loose = self.card(image=url)
        self.card(image=self.upload().get_json()["url"], tags=["recipes"])
        self.assertEqual([c["id"] for c in self.cards("?unsorted=1")], [loose["id"]])


class Security(Base):
    def test_cross_site_writes_are_refused(self):
        evil = self.c.post("/api/cards", json={"text": "x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(evil.status_code, 403)
        null = self.c.post("/api/cards", json={"text": "x"}, headers={"Origin": "null"})
        self.assertEqual(null.status_code, 403)
        same = self.c.post("/api/cards", json={"text": "x"}, headers={"Origin": "http://localhost"})
        self.assertEqual(same.status_code, 201)
        self.assertEqual(len(self.cards()), 1)  # only the same-site one was saved


def fake_assets(ids):
    return [{"id": i, "fileCreatedAt": "2026-01-01T10:00:00.000Z"} for i in ids]


class ImmichSync(Base):
    def setUp(self):
        super().setUp()
        self.ids = [str(uuid.uuid4()) for _ in range(3)]
        self.pages = {None: (fake_assets(self.ids[:2]), "next"), "next": (fake_assets(self.ids[2:]), None)}
        p = [mock.patch.object(immich, "URL", "http://immich.test"), mock.patch.object(immich, "KEY", "k"),
             mock.patch.object(immich, "_search_page", lambda cursor=None, size=100: self.pages[cursor])]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def sync(self):
        return self.c.post("/api/immich/sync").get_json()

    def test_imports_every_page_once(self):
        r = self.sync()
        self.assertEqual((r["ok"], r["imported"], r["scanned"]), (True, 3, 3))
        self.assertEqual(self.sync()["imported"], 0)
        self.assertTrue(all(c["tags"] == ["screenshot"] for c in self.cards()))

    def test_a_deleted_screenshot_is_never_imported_again(self):
        self.sync()
        victim = next(c for c in self.cards())
        self.c.delete(f"/api/cards/{victim['id']}")
        self.assertEqual(self.sync()["imported"], 0)
        self.assertEqual(len(self.cards()), 2)

    def test_older_screenshots_that_arrive_late_are_still_found(self):
        self.sync()
        late = str(uuid.uuid4())
        self.pages[None] = (fake_assets(self.ids[:2]), "next")
        self.pages["next"] = (fake_assets(self.ids[2:] + [late]), None)  # an old one uploaded after newer ones
        self.assertEqual(self.sync()["imported"], 1)

    def test_not_configured_is_reported_not_crashed(self):
        with mock.patch.object(immich, "KEY", ""):
            r = self.c.post("/api/immich/sync")
            self.assertEqual(r.status_code, 502)
            self.assertFalse(r.get_json()["ok"])


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Stands in for an authorized Google session."""

    def __init__(self):
        self.calls, self.events, self.gone, self.down, self.n = [], {}, set(), False, 0

    def post(self, url, json=None, timeout=None):
        if self.down:
            raise ConnectionError("Google is down")
        self.n += 1
        eid = f"evt{self.n}"
        self.events[eid] = json
        self.calls.append(("post", eid))
        return FakeResp(200, {"id": eid})

    def patch(self, url, json=None, timeout=None):
        eid = url.rsplit("/", 1)[-1]
        if eid in self.gone:
            return FakeResp(404)
        self.events[eid] = json
        self.calls.append(("patch", eid))
        return FakeResp(200, {"id": eid})

    def delete(self, url, timeout=None):
        eid = url.rsplit("/", 1)[-1]
        self.events.pop(eid, None)
        self.calls.append(("delete", eid))
        return FakeResp(204)


class CalendarSync(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeSession()
        p = [mock.patch.object(gcal, "CAL_ID", "cal@example.test"), mock.patch.object(gcal, "configured", lambda: True),
             mock.patch.object(gcal, "_session", self.fake)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def reminder(self, text="Pay rent"):
        return self.card(text=text, tags=["reminder"], remind_at="2030-05-01T09:30")

    def test_reminder_creates_updates_and_deletes_an_event(self):
        c = self.reminder()
        self.assertTrue(c["on_calendar"])
        self.assertEqual(self.fake.calls[0][0], "post")
        body = self.fake.events[self.fake.calls[0][1]]
        self.assertEqual(body["start"]["dateTime"], "2030-05-01T09:30:00")
        self.c.patch(f"/api/cards/{c['id']}", json={"text": "Pay rent today"})
        self.assertEqual(self.fake.calls[-1][0], "patch")
        self.c.delete(f"/api/cards/{c['id']}")
        self.assertEqual(self.fake.calls[-1][0], "delete")

    def test_event_deleted_on_the_calendar_side_is_recreated(self):
        c = self.reminder()
        self.fake.gone.add(self.fake.calls[0][1])
        self.c.patch(f"/api/cards/{c['id']}", json={"text": "Pay rent again"})
        self.assertEqual([k for k, _ in self.fake.calls], ["post", "post"])

    def test_notes_never_touch_the_calendar(self):
        self.card(text="just a note")
        self.assertEqual(self.fake.calls, [])

    def test_a_google_outage_never_blocks_saving_and_is_retried(self):
        self.fake.down = True
        c = self.reminder("Call the bank")
        self.assertFalse(c["on_calendar"])  # saved, just not on the calendar yet
        self.fake.down = False
        A.retry_calendar()
        self.assertTrue(self.cards()[0]["on_calendar"])


class Pages(Base):
    def test_home_page_loads(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"ADHDpls", r.data)
        r.close()


if __name__ == "__main__":
    unittest.main()
