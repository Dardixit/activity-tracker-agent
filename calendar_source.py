"""Read events from the Google Calendar in GOOGLE_CALENDAR_ID (read-only).

The calendar is shared with the service account ("See all event details"),
so the same key file as for the Sheet is used, with a read-only calendar scope.

Each event is turned into a plain dict:
    {"id", "title", "start", "end" (aware datetimes in Helsinki time; for all-day
     events midnight), "all_day", "cancelled", "declined", "location",
     "description" (at most 300 characters), "recurring_id"}
Recurring events come as one dict per occurrence.
"""

import os
import re
from datetime import date, datetime, time, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

import agent
import retry
import sheets

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
WINDOW_MONTHS = 2
DESCRIPTION_LIMIT = 300


def service():
    key = sheets.find_key_file()
    creds = service_account.Credentials.from_service_account_file(str(key), scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def add_months(d, months):
    """The same day `months` later (the month's last day if it is shorter)."""
    month = d.month - 1 + months
    year, month = d.year + month // 12, month % 12 + 1
    for day in range(d.day, 27, -1):
        try:
            return d.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    return d.replace(year=year, month=month, day=min(d.day, 28))


def window():
    """(start, end): today 00:00 to the same day WINDOW_MONTHS later, Helsinki time."""
    start = datetime.combine(agent.now().date(), time(0), agent.TIMEZONE)
    return start, add_months(start, WINDOW_MONTHS)


def _when(value):
    """A start/end field as (aware datetime in Helsinki time, is_all_day)."""
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"]).astimezone(agent.TIMEZONE), False
    return datetime.combine(date.fromisoformat(value["date"]), time(0), agent.TIMEZONE), True


def _clean(text, limit):
    text = re.sub(r"<[^>]+>", " ", text or "")  # descriptions can hold HTML
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def normalise(item):
    """One API event as the dict described above. Attendee emails, meeting
    links and attachments are left out on purpose: they never go to the model."""
    cancelled = item.get("status") == "cancelled"
    start_field = item.get("start") or item.get("originalStartTime") or {}
    if not start_field:
        return None
    start, all_day = _when(start_field)
    end = _when(item["end"])[0] if item.get("end") else start
    me = [a for a in item.get("attendees", []) if a.get("self")]
    return {
        "id": item["id"],
        "title": item.get("summary", "").strip(),
        "start": start,
        "end": end,
        "all_day": all_day,
        "cancelled": cancelled,
        "declined": bool(me) and me[0].get("responseStatus") == "declined",
        "location": _clean(item.get("location"), 120),
        "description": _clean(item.get("description"), DESCRIPTION_LIMIT),
        "recurring_id": item.get("recurringEventId"),
    }


def fetch(cal=None):
    """Every event in the window, cancelled ones included, one per occurrence."""
    cal = cal or service()
    start, end = window()
    items, token = [], None
    while True:
        page = retry.execute(cal.events().list(
            calendarId=CALENDAR_ID, timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", showDeleted=True, maxResults=250, pageToken=token,
        ), "Calendar read")
        items += page.get("items", [])
        token = page.get("nextPageToken")
        if not token:
            break
    events = [e for e in (normalise(i) for i in items) if e]
    # A cancelled occurrence of a recurring event comes without its title.
    titles = {e["recurring_id"]: e["title"] for e in events if e["recurring_id"] and e["title"]}
    for e in events:
        if not e["title"] and e["recurring_id"] in titles:
            e["title"] = titles[e["recurring_id"]]
    return events


def get(event_id, cal=None):
    """One event by id (for linked events that moved outside the window), or None.

    A network error is raised, not taken as None: None makes the row's event
    count as cancelled."""
    cal = cal or service()
    try:
        return normalise(retry.execute(cal.events().get(calendarId=CALENDAR_ID, eventId=event_id), "Calendar read"))
    except Exception as err:
        if retry.is_network_error(err):
            raise
        return None


def date_text(event):
    """The event's date as the Sheet writes it.

    "26.10.2026, 17.00" for a timed event, "26.10.2026" for a one-day all-day
    event, "21-22.10.2026" for a multi-day event within one month (the first
    date only when it spans months).
    """
    start = event["start"]
    if not event["all_day"]:
        return f"{start:%d.%m.%Y}, {start:%H.%M}"
    last = (event["end"] - timedelta(days=1)).date()  # all-day end dates are exclusive
    if last <= start.date():
        return f"{start:%d.%m.%Y}"
    if (last.year, last.month) == (start.year, start.month):
        return f"{start:%d}-{last:%d.%m.%Y}"
    return f"{start:%d.%m.%Y}"


def has_ended(event):
    return event["end"] <= agent.now()


def moved(event, row_date):
    """Does the row's Date disagree with the event's start?

    The event's day must be within the row's dates (a "28-29.10.2026" row
    covers both days); the time is compared only when the row has one, so a
    hand-entered "26.10.2026" for a 17.00 event is not a move.
    """
    span = agent.date_span(row_date)
    if span is None or not span[0] <= event["start"].date() <= span[1]:
        return True
    m = re.search(r",\s*(\d{1,2})[.:](\d{2})", row_date or "")
    if m and not event["all_day"] and span[0] == span[1]:
        return (int(m.group(1)), int(m.group(2))) != (event["start"].hour, event["start"].minute)
    return False
