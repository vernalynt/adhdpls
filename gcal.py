"""Google Calendar sync for #reminder cards (service account, REST API).

Config (environment):
  GCAL_CALENDAR_ID        calendar to write to (the calendar's ID, shown in its settings)
  GCAL_CREDENTIALS_FILE   service account JSON key (default /secrets/gcal.json)
  GCAL_TIMEZONE           IANA zone for reminder times (default UTC)

The service account must be shared on that calendar with "Make changes to events".
The key file stays on the server and is never copied into the image.
"""
import os
from datetime import datetime, timedelta
from urllib.parse import quote

CAL_ID = os.environ.get("GCAL_CALENDAR_ID", "")
CRED_FILE = os.environ.get("GCAL_CREDENTIALS_FILE", "/secrets/gcal.json")
TZ = os.environ.get("GCAL_TIMEZONE", "UTC")
DURATION_MIN = 15
BASE = "https://www.googleapis.com/calendar/v3/calendars/"
SCOPE = "https://www.googleapis.com/auth/calendar"

_session = None


def configured():
    return bool(CAL_ID and os.path.isfile(CRED_FILE))


def _get_session():
    global _session
    if _session is None:
        # Imported here so the app still starts when Calendar is not set up.
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(CRED_FILE, scopes=[SCOPE])
        _session = AuthorizedSession(creds)
    return _session


def _body(summary, remind_at):
    start = datetime.fromisoformat(remind_at)
    end = start + timedelta(minutes=DURATION_MIN)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return {
        "summary": (summary or "Reminder")[:200],
        "description": "Created by ADHDpls",
        "start": {"dateTime": start.strftime(fmt), "timeZone": TZ},
        "end": {"dateTime": end.strftime(fmt), "timeZone": TZ},
        # popup at the start time, so the phone pings right when the reminder is due
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
    }


def _events_url():
    return BASE + quote(CAL_ID, safe="") + "/events"


def upsert(event_id, summary, remind_at):
    """Create the event, or update it if event_id exists. Returns the event id."""
    s, body = _get_session(), _body(summary, remind_at)
    if event_id:
        r = s.patch(f"{_events_url()}/{quote(event_id, safe='')}", json=body, timeout=15)
        if r.status_code in (404, 410):  # deleted on the calendar side: recreate
            event_id = None
        else:
            r.raise_for_status()
            return r.json()["id"]
    r = s.post(_events_url(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def delete(event_id):
    r = _get_session().delete(f"{_events_url()}/{quote(event_id, safe='')}", timeout=15)
    if r.status_code not in (200, 204, 404, 410):
        r.raise_for_status()
