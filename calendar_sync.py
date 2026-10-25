"""Google Calendar as a second source for the Activity Log.

check() runs at bot start and then hourly (when CALENDAR_SYNC=on in .env):
1. Read the calendar window (today to 2 months ahead) and the Sheet.
2. Events already linked to a row (its Event ID cell): a moved event updates
   the row's Date; a cancelled one asks whether to set Outcome "Cancelled";
   an event that has ended while its row is still Confirmed asks whether you
   attended (yes -> Done, no -> Missed).
3. Unlinked events: declined events and titles marked "no" in the hidden
   Calendar titles tab are skipped. The rest go to the model in batches: it
   links each event to an existing row, adds a new row (Status Confirmed),
   decides the title, or asks you.
4. Writes go through sheet_writer.Writer.apply, the same code as Telegram notes,
   so new rows are inserted in date order with dropdowns. Every reason starts
   with "calendar:".
5. Each question goes out as its own Telegram message with buttons (bot.py).
   A tap, a free-text reply, or a numbered list of answers you confirm is
   turned into writes by answer(). Skip means "not now": the question comes
   back after SNOOZE_DAYS.

check() itself never writes: apply() and answer() do.

Practice run, nothing written or sent; the decisions are saved to calendar_plan.json:
    .venv/bin/python calendar_sync.py --practice
After you approve that report, write exactly those decisions (no new model
decisions; every cell is still checked against the Sheet first) and send its questions:
    .venv/bin/python calendar_sync.py --apply-plan
"""

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import agent
import calendar_source as cs
import retry
import state as agent_state
import sheets
import sheet_writer

TITLES_TAB = "Calendar titles"
TITLES_HEADER = ["Title", "Track", "Decided by", "Date"]
SOURCE_VALUE = "calendar"
MAX_QUESTIONS = 15          # per Telegram message; the rest wait for the next check
BATCH = 25                  # events per model call
CANDIDATE_DAYS = 3          # rows this many days either side are shown as possible matches
MAX_TURNS = 8
SNOOZE_DAYS = 7             # a skipped question comes back after this many days

# Per-event memory that has no place in the tracker tabs: cancellations you
# said not to mark, and events you said to leave out. Kept in this file, or in
# the Agent state tab with STATE_BACKEND=sheet (state.py). (Ignored by git
# through *.json.)
STATE_FILE = Path(__file__).with_name("calendar_state.json")
# The decisions of a practice run, waiting for approval.
PLAN_FILE = Path(__file__).with_name("calendar_plan.json")
PLAN_MAX_AGE = timedelta(hours=24)


# ------------------------------------------------------------------ Loading

def title_key(title):
    return " ".join(title.lower().split())


def load_state():
    if agent_state.in_sheet():
        state = agent_state.store().get("calendar_state", {})
    else:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    # snoozed: {question key: date (ISO) before which it is not asked again}
    return {"ignore_cancel": [], "ignore_event": [], "snoozed": {}, **state}


def snoozed(state, key):
    until = state["snoozed"].get(key)
    return until is not None and agent.now().date() < date.fromisoformat(until)


def save_state(state):
    if agent_state.in_sheet():
        agent_state.store().put({"calendar_state": state})
        return
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def load_data():
    """The Sheet as the agent reads it, plus the Calendar titles tab (if set up)."""
    creds, _ = sheets.load_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    data = agent.load_sheet()
    try:
        rows = agent.read_tabs(service, [TITLES_TAB])[0]
    except HttpError:
        rows = []  # not set up yet (practice run before setup_sheet.py)
    return data, rows


def titles_from(rows):
    """{title key: {"row", "Title", "Track", "Decided by"}} from the tab rows."""
    if not rows:
        return {}
    header = rows[0]
    out = {}
    for n, values in enumerate(rows[1:], start=2):
        r = agent.row_as_dict(header, values)
        if r.get("Title", "").strip():
            out[title_key(r["Title"])] = {**r, "row": n}
    return out


def row_dict(data, row):
    tab = data[agent.ACTIVITY]
    return agent.row_as_dict(tab["header"], tab["rows"][row - 2])


def row_event_ids(r):
    """The event ids in a row's Event ID cell. A multi-day row ("28-29.10.2026")
    can hold one id per day, comma-separated, when the calendar has an event per day."""
    return [i.strip() for i in r.get(agent.EVENT_ID, "").split(",") if i.strip()]


def linked_rows(data):
    """{event id: Sheet row} for rows with an Event ID."""
    tab = data[agent.ACTIVITY]
    if agent.EVENT_ID not in tab["header"]:
        return {}
    out = {}
    for n, values in enumerate(tab["rows"], start=2):
        for eid in row_event_ids(agent.row_as_dict(tab["header"], values)):
            out[eid] = n
    return out


def covers(r, event):
    """Is the event's day within the row's dates?"""
    span = agent.date_span(r["Date"])
    return span is not None and span[0] <= event["start"].date() <= span[1]


def is_range(r):
    span = agent.date_span(r["Date"])
    return span is not None and span[0] != span[1]


def candidates(data, event, taken):
    """Rows that might already be this event: within CANDIDATE_DAYS of it, or
    sharing a title word within two weeks. Linked rows are left out."""
    day = event["start"].date()
    title_words = agent.words(event["title"])
    tab = data[agent.ACTIVITY]
    out = []
    for n, values in enumerate(tab["rows"], start=2):
        r = agent.row_as_dict(tab["header"], values)
        span = agent.date_span(r["Date"])
        if not span:
            continue
        # A linked row is only a candidate as another day of a multi-day row.
        if (n in taken or row_event_ids(r)) and not (is_range(r) and covers(r, event)):
            continue
        gap = 0 if span[0] <= day <= span[1] else min(abs((span[0] - day).days), abs((span[1] - day).days))
        shares = any(agent.term_matches(w, agent.words(r["Activity"] + " " + r["Description"])) for w in title_words)
        if gap <= CANDIDATE_DAYS or (shares and gap <= 14):
            out.append({"row": n, "same_day": gap == 0, **{k: r[k] for k in
                        ("Date", "Status", "Category", "Activity", "Description", "Outcome")},
                        "locked": agent.is_locked(data, n)})
    return out[:10]


# ----------------------------------------------------------------- Questions

def question_text(item, hints=True):
    """The question in words. hints=False leaves out the "(yes/no)" part, for
    messages that come with buttons."""
    if item["kind"] == "title":
        count = f" ({item['count']} in the next 2 months, first on {item['date']})" if item.get("count") else ""
        text, hint = f'Should calendar events titled "{item["title"]}"{count} go in the Activity Log?', "(yes/no)"
    elif item["kind"] == "match":
        text = f'Is "{item["title"]}" on {item["date"]} the same as row "{item["entry"]}"?'
        hint = "(yes = same row / no = add it as a new row / skip = ask again in a week)"
    elif item["kind"] == "same_time":
        text = (f'Calendar event "{item["title"]}" on {item["date"]} starts at the same time as row '
                f'"{item["entry"]}", which is already linked to the event "{item["other_title"]}". Is it the same event?')
        hint = "(yes = same event, link it to that row / no = different event, add a row / skip = ask again in a week)"
    elif item["kind"] == "attend":
        text, hint = f'Did you attend "{item["activity"]}" ({item["date"]})?', "(yes = Done / no = Missed)"
    elif item["kind"] == "cancel":
        text = (f'"{item["title"]}" on {item["date"]} was cancelled in the calendar. '
                f'Set Outcome to "Cancelled" on row "{item["entry"]}"?')
        hint = "(yes/no)"
    else:
        raise ValueError(item["kind"])
    return f"{text} {hint}" if hints else text


# Button labels per answer. Skip is always "not now".
BUTTONS = {"match": [("Same row", "yes"), ("New row", "no"), ("Skip", "skip")],
           "same_time": [("Same event, link to existing row", "yes"), ("Different event, add row", "no"),
                         ("Skip", "skip")]}
DEFAULT_BUTTONS = [("Yes", "yes"), ("No", "no"), ("Skip", "skip")]


def buttons(item):
    return BUTTONS.get(item["kind"], DEFAULT_BUTTONS)


def answer_label(item, answer):
    """The button text for an answer, e.g. "Same row"."""
    return next((label for label, value in buttons(item) if value == answer), answer)


def single_question(item, number, total):
    """The text of one question message."""
    return f"Calendar question {number} of {total}:\n\n{question_text(item, hints=False)}"


# A numbered list of answers: "1 yes, 2 no" or one "3. Yes" per line.
ANSWER_WORDS = {"yes": "yes", "y": "yes", "kyllä": "yes", "same": "yes", "same row": "yes",
                "no": "no", "n": "no", "ei": "no", "new": "no", "new row": "no",
                "skip": "skip", "later": "skip"}
NUMBERED_RE = re.compile(r"(\d{1,2})\s*[.):\-]?\s*(same row|new row|kyllä|[a-zä]+)", re.I)


def numbered_answers(text):
    """{number: "yes"/"no"/"skip"} if the text is a list of at least two numbered
    answers (and little else), else {}."""
    found = {int(n): ANSWER_WORDS.get(w.lower()) for n, w in NUMBERED_RE.findall(text)}
    found = {n: a for n, a in found.items() if a}
    leftover = NUMBERED_RE.sub("", text)
    if len(found) >= 2 and len(re.findall(r"[^\W\d_]+", leftover)) <= 3:
        return found
    return {}


def item_key(item):
    if item["kind"] == "title":
        return f"title:{title_key(item['title'])}"
    return f"{item['kind']}:{item['event_id']}"


def question_message(items):
    """All questions in one text (the practice report; old-style batch messages)."""
    lines = ["Calendar questions. Reply to this message, e.g. \"1 no, 2 yes\". Nothing is written until you answer."]
    lines += [f"{i}. {question_text(item)}" for i, item in enumerate(items, 1)]
    return "\n\n".join(lines)


# --------------------------------------------------------------- Model tools

def tools(categories):
    event = {"type": "string", "description": "Event id from the list, e.g. \"E3\"."}
    return [
        {"name": "link_event", "description":
            "The event is already in the Activity Log as this row (same day). Writes the event's id into the row.",
         "input_schema": {"type": "object", "properties": {
             "event": event, "row": {"type": "integer"}, "reason": {"type": "string"}},
             "required": ["event", "row", "reason"], "additionalProperties": False}},
        {"name": "add_row", "description":
            "The event belongs in the tracker and no row covers it: add a row (Status Confirmed, date from the event).",
         "input_schema": {"type": "object", "properties": {
             "event": event, "category": {"type": "string", "enum": categories},
             "activity": {"type": "string"}, "description": {"type": "string"}, "reason": {"type": "string"}},
             "required": ["event", "category", "activity", "description", "reason"], "additionalProperties": False}},
        {"name": "decide_title", "description":
            "You are confident whether events with this title belong in the tracker. Remembered for every "
            "event with this title, now and later. track=false also settles all its events in this list.",
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"}, "track": {"type": "boolean"}, "reason": {"type": "string"}},
             "required": ["title", "track", "reason"], "additionalProperties": False}},
        {"name": "ask_title", "description":
            "You are unsure whether events with this title belong in the tracker. Asks the user once for the title.",
         "input_schema": {"type": "object", "properties": {"title": {"type": "string"}},
                          "required": ["title"], "additionalProperties": False}},
        {"name": "ask_match", "description":
            "A row might be this event but you are not sure (another day, or very different wording). Asks the "
            "user. Give the new-row fields to use if the user says it is a different event.",
         "input_schema": {"type": "object", "properties": {
             "event": event, "row": {"type": "integer"}, "category": {"type": "string", "enum": categories},
             "activity": {"type": "string"}, "description": {"type": "string"}},
             "required": ["event", "row", "category", "activity", "description"], "additionalProperties": False}},
    ]


def system_prompt(data):
    return f"""You connect Google Calendar events to an activity tracker's {agent.ACTIVITY} in a Google Sheet.
The tracker records these kinds of activity: {", ".join(c.lower() for c in agent.CATEGORIES)}.
Private appointments (commuting, time blocks, "busy", errands, family, health) do not belong in it.

Each event in the list needs exactly one of:
- link_event: a row already covers this event on the same day. Many rows were entered by hand with
  different wording (e.g. calendar "Finnish language café" = row "Language café at the library").
- add_row: the event belongs in the tracker and no row covers it.
- decide_title with track=false: events with this title do not belong (settles all of them).
- ask_title: you are not sure whether events with this title belong.
- ask_match: a row might be this event, but you are not sure (another day, or very different wording).
When you are confident a title belongs, also call decide_title with track=true, so it is remembered.
Titles marked "yes" below are already decided as belonging: link or add those events.

Rules:
- Never add a row for an event that a row already covers. When in doubt between link and add, use ask_match.
- An event that starts at the same date and time as a row already linked to another event is not added:
  the code asks the user instead, so add_row for it returns "Not added". That is not an error.
- Category must be one of: {agent.allowed_values(data, "Category")}.
- Activity is a short name like the existing rows, based on the event title; Description a short phrase.
  Never put the date or time in Activity or Description: the Date column has them.
- Only use names that appear in the event's title, location or description. Never invent people or companies.
- Recurring events come as one item per occurrence: each needs its own decision (usually the same kind).
- Rows marked locked are read only: you cannot link them."""


class Batch:
    """One model run over a list of events, collecting the decisions in `plan`."""

    def __init__(self, data, events, titles, plan, starts=None):
        self.data, self.titles, self.plan = data, titles, plan
        # {start datetime: [(row, event)]} of the events already linked to rows.
        self.starts = starts or {}
        self.ids = {f"E{i}": e for i, e in enumerate(events, 1)}
        self.open = set(self.ids)              # events still needing a decision
        self.taken = {row for _, row, _ in plan["links"]} | {i["row"] for i in plan["questions"] if i["kind"] == "match"}
        self.dup_warned = set()

    def listing(self):
        known = sorted(t["Title"] for t in self.titles.values() if t.get("Track") == "yes")
        lines = [f"Titles already decided as belonging: {known}" if known else "No titles decided yet.", "", "Events:"]
        for eid, e in self.ids.items():
            info = {"event": eid, "title": e["title"], "date": cs.date_text(e), "location": e["location"],
                    "description": e["description"], "candidate_rows": candidates(self.data, e, self.taken)}
            lines.append(json.dumps(info, ensure_ascii=False))
        return "\n".join(lines)

    def settle_title(self, title):
        for eid in list(self.open):
            if title_key(self.ids[eid]["title"]) == title_key(title):
                self.open.discard(eid)

    def fields(self, e, args):
        return {"Date": cs.date_text(e), "Status": "Confirmed", "Category": args["category"],
                "Activity": args["activity"].strip(), "Description": args["description"].strip() or "-",
                "Outcome": "-"}

    def same_start(self, e):
        """(row, other event) when a row is already linked to another event
        that starts at the same moment as e (a timed event), else None. Links
        made earlier in this run count too."""
        if e.get("all_day") or not e.get("start"):
            return None
        linked = list(self.starts.get(e["start"], []))
        linked += [(row, other) for other, row, _ in self.plan["links"] if other.get("start") == e["start"]]
        return next(((row, other) for row, other in linked if other["id"] != e["id"]), None)

    def check_fields(self, e, fields):
        change = {"action": "add", "tab": agent.ACTIVITY, "row": 0, "fields": fields}
        return agent.check_change(self.data, change, texts=[e["title"], e["location"], e["description"]])

    def run_tool(self, name, args):
        """Returns (result text, is_error)."""
        if name in ("decide_title", "ask_title"):
            title = args["title"].strip()
            known = self.titles.get(title_key(title))
            if known and known.get("Decided by") == "you":
                return f"The user already decided {title!r}: {known['Track']}. Follow that.", True
            if name == "decide_title":
                self.plan["titles"].append({"title": title, "track": args["track"], "by": "model",
                                            "reason": args["reason"]})
                self.titles[title_key(title)] = {"Title": title, "Track": "yes" if args["track"] else "no",
                                                 "Decided by": "model"}
                if not args["track"]:
                    self.settle_title(title)
                return "Recorded.", False
            matching = [e for e in self.ids.values() if title_key(e["title"]) == title_key(title)]
            if not matching:
                return f"No event in the list has the title {title!r}.", True
            first = min(matching, key=lambda e: e["start"])
            self.plan["questions"].append({"kind": "title", "title": title, "count": len(matching),
                                           "date": f"{first['start']:%d.%m.%Y}"})
            self.settle_title(title)
            return "The user will be asked.", False

        eid = args["event"]
        e = self.ids.get(eid)
        if e is None or eid not in self.open:
            return f"{eid} is not an open event in the list.", True
        if name in ("link_event", "ask_match"):
            row = args["row"]
            if not 2 <= row < len(self.data[agent.ACTIVITY]["rows"]) + 2:
                return f"Row {row} does not exist.", True
            r = row_dict(self.data, row)
            if (row_event_ids(r) or row in self.taken) and not (is_range(r) and covers(r, e)):
                return f"Row {row} is already linked to another event.", True
            if agent.is_locked(self.data, row):
                return f"Row {row} is locked (read only), so it can't be linked.", True
        if name == "link_event":
            if not covers(r, e):
                return (f"Row {row} is on {r['Date']}, not on the event's day ({cs.date_text(e)}). "
                        "If you still think it is the same event, use ask_match."), True
            self.plan["links"].append((e, row, args["reason"]))
            self.taken.add(row)
        elif name == "add_row":
            fields = self.fields(e, args)
            problems = self.check_fields(e, fields)
            if problems:
                return "Rejected:\n- " + "\n- ".join(problems), True
            # Same date and start time as a row already linked to another event:
            # never added without asking (a calendar often holds two entries for
            # one meeting).
            clash = self.same_start(e)
            if clash:
                row, other = clash
                r = row_dict(self.data, row)
                self.plan["questions"].append({
                    "kind": "same_time", "event_id": e["id"], "title": e["title"], "date": cs.date_text(e),
                    "row": row, "entry": sheet_writer.entry_for(agent.ACTIVITY, r), "other_title": other["title"],
                    "fields": fields})
                self.open.discard(eid)
                return (f"Not added: row {row} \"{r['Activity']}\" is already linked to the event "
                        f"{other['title']!r}, which starts at the same time. The user will be asked."), False
            # A same-day row sharing a title word looks like a duplicate: make
            # the model look again once before adding.
            similar = [c for c in candidates(self.data, e, self.taken) if c["same_day"] and any(
                agent.term_matches(w, agent.words(c["Activity"] + " " + c["Description"])) for w in agent.words(e["title"]))]
            if similar and eid not in self.dup_warned:
                self.dup_warned.add(eid)
                rows = ", ".join(f"row {c['row']} \"{c['Activity']}\"" for c in similar)
                return (f"Possible duplicate: {rows} is on the same day and shares a word with the title. "
                        "Link it if it is the same event, use ask_match if unsure, or call add_row again "
                        "if it really is a different event."), True
            self.plan["adds"].append((e, fields, args["reason"]))
        else:  # ask_match
            fields = self.fields(e, args)
            problems = self.check_fields(e, fields)
            if problems:
                return "Rejected (the fallback row fields):\n- " + "\n- ".join(problems), True
            self.plan["questions"].append({
                "kind": "match", "event_id": e["id"], "title": e["title"], "date": cs.date_text(e),
                "row": row, "entry": sheet_writer.entry_for(agent.ACTIVITY, r), "fields": fields})
            self.taken.add(row)
        self.open.discard(eid)
        return "Recorded.", False

    def run(self, client):
        messages = [{"role": "user", "content": self.listing()}]
        nudged = False
        for _ in range(MAX_TURNS):
            response = retry.call("Claude", lambda: client.messages.create(
                model=agent.MODEL, max_tokens=16000, output_config={"effort": "medium"},
                system=system_prompt(self.data), tools=tools(agent.allowed_values(self.data, "Category")),
                messages=messages,
            ))
            if response.stop_reason in ("refusal", "max_tokens"):
                self.plan["notes"].append(f"Model stopped early ({response.stop_reason}).")
                break
            calls = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})
            if not calls:
                if self.open and not nudged:
                    nudged = True
                    messages.append({"role": "user", "content":
                        f"These events still need a decision: {sorted(self.open)}. Decide each one."})
                    continue
                break
            results = []
            for c in calls:
                text, is_error = self.run_tool(c.name, c.input)
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            if not self.open:
                break
        for eid in self.open:
            self.plan["undecided"].append(self.ids[eid])


# ---------------------------------------------------------------- The check

def new_plan():
    return {"links": [], "adds": [], "moves": [], "titles": [], "questions": [], "skipped": [],
            "undecided": [], "notes": []}


def check(client, practice=False, open_keys=(), events=None, data=None, title_rows=None, get_event=None):
    """Work out what the calendar means for the Sheet. Returns the plan.

    events/data/title_rows/get_event can be passed in by tests; otherwise the
    calendar and the Sheet are read here.
    """
    if data is None:
        data, title_rows = load_data()
    titles = titles_from(title_rows or [])
    events = cs.fetch() if events is None else events
    get_event = get_event or cs.get
    state = load_state()
    plan = new_plan()
    open_keys = set(open_keys)
    today = agent.now().date()
    by_id = {e["id"]: e for e in events}
    linked = linked_rows(data)
    starts = {}
    for eid, row in linked.items():
        e = by_id.get(eid)
        if e and not e["all_day"] and e["start"]:
            starts.setdefault(e["start"], []).append((row, e))

    # 1. Events already linked to a row.
    ended_by_row = {}
    for eid, row in linked.items():
        r = row_dict(data, row)
        key = agent.date_key(r["Date"])
        e = by_id.get(eid)
        if e is None and key and type(today)(*key) >= today:
            # A future row whose event is not in the window: moved out of it, or deleted.
            e = get_event(eid) or {"id": eid, "title": r["Activity"], "cancelled": True,
                                   "start": None, "all_day": False}
        entry = sheet_writer.entry_for(agent.ACTIVITY, r)
        if e and e["cancelled"]:
            if "cancelled" not in r["Outcome"].lower() and eid not in state["ignore_cancel"]:
                plan["questions"].append({"kind": "cancel", "event_id": eid, "title": e["title"] or r["Activity"],
                                          "date": r["Date"], "entry": entry, "row": row})
            continue
        if e and not agent.is_locked(data, row) and cs.moved(e, r["Date"]):
            plan["moves"].append((e, row, r["Date"], cs.date_text(e)))
        ended = (e is not None and cs.has_ended(e)) or (key is not None and type(today)(*key) < today)
        ended_by_row.setdefault(row, []).append((eid, ended))
    # One attendance question per row, once all its events have ended.
    for row, events_of_row in ended_by_row.items():
        r = row_dict(data, row)
        if r["Status"] == "Confirmed" and all(ended for _, ended in events_of_row):
            plan["questions"].append({"kind": "attend", "event_id": events_of_row[-1][0], "activity": r["Activity"],
                                      "date": r["Date"], "entry": sheet_writer.entry_for(agent.ACTIVITY, r)})

    # 2. Unlinked events: which ones the model needs to look at.
    to_decide = []
    for e in events:
        why = None
        if e["id"] in linked:
            continue
        if e["cancelled"]:
            why = "cancelled"
        elif e["declined"]:
            why = "declined by you"
        elif e["id"] in state["ignore_event"]:
            why = "you said to leave it out"
        elif titles.get(title_key(e["title"]), {}).get("Track") == "no":
            why = f"title marked no ({titles[title_key(e['title'])]['Decided by']})"
        elif f"title:{title_key(e['title'])}" in open_keys:
            why = "title question already open"
        elif snoozed(state, f"title:{title_key(e['title'])}"):
            why = "title question skipped for now"
        elif snoozed(state, f"match:{e['id']}"):
            why = "match question skipped for now"
        elif f"match:{e['id']}" in open_keys:
            why = "match question already open"
        elif snoozed(state, f"same_time:{e['id']}"):
            why = "same-time question skipped for now"
        elif f"same_time:{e['id']}" in open_keys:
            why = "same-time question already open"
        if why:
            plan["skipped"].append((e, why))
        else:
            to_decide.append(e)

    # 3. The model, in batches; titles decided "no" in one batch skip the next.
    while to_decide and client is not None:
        batch, to_decide = to_decide[:BATCH], to_decide[BATCH:]
        Batch(data, batch, titles, plan, starts).run(client)
        settled = {title_key(q["title"]) for q in plan["questions"] if q["kind"] == "title"}
        still = []
        for e in to_decide:
            k = title_key(e["title"])
            if titles.get(k, {}).get("Track") == "no":
                plan["skipped"].append((e, "title marked no (model)"))
            elif k in settled:
                plan["skipped"].append((e, "title question asked"))
            else:
                still.append(e)
        to_decide = still

    # 4. Recurring events and repeated titles: new rows copy Activity, Category
    #    and Description from the first row of that series or title, preferring a
    #    row you entered by hand, so they read the same as the rows already there.
    series_rows = {}
    for e in events:
        row = linked.get(e["id"])
        if row:
            for key in (e["recurring_id"], title_key(e["title"])):
                if key:
                    series_rows.setdefault(key, []).append(row)
    for e, row, _ in plan["links"]:
        for key in (e["recurring_id"], title_key(e["title"])):
            if key:
                series_rows.setdefault(key, []).append(row)
    first_add = {}
    for e, fields, _ in sorted(plan["adds"], key=lambda a: a[0]["start"]):
        keys = [k for k in (e["recurring_id"], title_key(e["title"])) if k]
        rows = next((series_rows[k] for k in keys if k in series_rows), [])
        template = {}
        if rows:
            manual = [r for r in rows if row_dict(data, r).get(agent.SOURCE) != SOURCE_VALUE]
            template = row_dict(data, min(manual or rows))
        earlier = next((first_add[k] for k in keys if k in first_add), {})
        for column in ("Category", "Activity", "Description"):
            if template:
                # Copy the series' existing row; an empty cell there becomes "-".
                fields[column] = template.get(column) or ("-" if column == "Description" else fields[column])
            elif earlier:
                fields[column] = earlier.get(column) or fields[column]
        for k in keys:
            first_add.setdefault(k, dict(fields))

    # 5. Rows being added for events that have already ended get an attendance question too.
    for e, fields, _ in plan["adds"]:
        if cs.has_ended(e):
            plan["questions"].append({"kind": "attend", "event_id": e["id"], "activity": fields["Activity"],
                                      "date": fields["Date"], "entry": sheet_writer.entry_for(agent.ACTIVITY, fields)})

    # Questions already waiting for an answer are not asked again; at most
    # MAX_QUESTIONS go out now, the rest at a later check.
    fresh = [q for q in plan["questions"] if item_key(q) not in open_keys and not snoozed(state, item_key(q))]
    plan["questions"], plan["later"] = fresh[:MAX_QUESTIONS], fresh[MAX_QUESTIONS:]
    plan["data"], plan["titles_rows"] = data, title_rows or []
    return plan


# ------------------------------------------------------------------- Writing

def title_edits(title_rows, decisions, today_text):
    """Edits that record title decisions in the Calendar titles tab."""
    header = title_rows[0] if title_rows else TITLES_HEADER
    existing = titles_from(title_rows)
    next_row = len(title_rows) + 1 if title_rows else 2
    edits = []
    for d in decisions:
        values = {"Title": d["title"], "Track": "yes" if d["track"] else "no", "Decided by": d["by"], "Date": today_text}
        known = existing.get(title_key(d["title"]))
        if known:
            row, action, current = known["row"], "update", known
        else:
            row, action, current = next_row, "add", {}
            next_row += 1
        for column, value in values.items():
            old = current.get(column, "")
            if action == "update" and old == value:
                continue
            edits.append({"tab": TITLES_TAB, "row": row, "col": header.index(column), "column": column,
                          "old": old, "new": value, "action": action, "entry": d["title"],
                          "reason": f"calendar: title decided by {d['by']}: {d['reason']}"})
    return edits


def cell_edit(data, row, column, new, reason):
    r = row_dict(data, row)
    header = data[agent.ACTIVITY]["header"]
    return {"tab": agent.ACTIVITY, "row": row, "col": header.index(column), "column": column, "old": r[column],
            "new": new, "action": "update", "entry": sheet_writer.entry_for(agent.ACTIVITY, r), "reason": reason}


def writes(plan):
    """(edits, inserts) for everything the plan decided."""
    data = plan["data"]
    edits, inserts = [], []
    by_row = {}
    for e, row, reason in plan["links"]:
        by_row.setdefault(row, []).append((e, reason))
    for row, links in by_row.items():
        ids = row_event_ids(row_dict(data, row)) + [e["id"] for e, _ in links]
        what = "; ".join(f'"{e["title"]}" ({cs.date_text(e)}): {reason}' for e, reason in links)
        edits.append(cell_edit(data, row, agent.EVENT_ID, ",".join(ids), f"calendar: matched to event {what}"))
    for e, row, old, new in plan["moves"]:
        edits.append(cell_edit(data, row, "Date", new, f'calendar: event "{e["title"]}" moved from {old} to {new}'))
    for e, fields, reason in plan["adds"]:
        values = {**fields, agent.SOURCE: SOURCE_VALUE, agent.LOCKED: False, agent.EVENT_ID: e["id"]}
        inserts.append({"tab": agent.ACTIVITY, "values": values,
                        "reason": f'calendar: event "{e["title"]}" ({fields["Date"]}): {reason}'})
    edits += title_edits(plan["titles_rows"], plan["titles"], agent.now().strftime("%d.%m.%Y"))
    return edits, inserts


def apply(writer, plan):
    """Write the plan. Returns a short summary for Telegram, or None if nothing changed."""
    edits, inserts = writes(plan)
    if not edits and not inserts:
        return None
    logged = writer.apply(edits, inserts)
    parts = []
    if inserts:
        parts.append("Added from the calendar:\n" + "\n".join(
            f"  row {i['row']}: {i['values']['Date']} {i['values']['Activity']}" for i in inserts))
    links = [e for e in edits if e["tab"] == agent.ACTIVITY and e["column"] == agent.EVENT_ID]
    if links:
        parts.append("Matched to existing rows:\n" + "\n".join(f"  {e['entry']}" for e in links))
    moves = [e for e in edits if e["tab"] == agent.ACTIVITY and e["column"] == "Date"]  # not the titles tab's Date
    if moves:
        parts.append("Moved in the calendar:\n" + "\n".join(f"  {e['entry']}: {e['old']} -> {e['new']}" for e in moves))
    parts.append(sheet_writer.logged_text(logged))
    return "Calendar check.\n\n" + "\n\n".join(parts)


# ------------------------------------------------------------------ Answers

ANSWER_TOOL = {
    "name": "resolve", "description": "Record the user's answer to one numbered question.",
    "input_schema": {"type": "object", "properties": {
        "item": {"type": "integer"}, "answer": {"type": "string", "enum": ["yes", "no", "skip"]}},
        "required": ["item", "answer"], "additionalProperties": False},
}


def read_answers(client, items, text, numbers=None):
    """{question number: "yes"/"no"/"skip"} from the user's reply, read by the model.
    numbers are the questions' own numbers (default 1, 2, ...)."""
    numbers = numbers or list(range(1, len(items) + 1))
    listing = "\n".join(f"{n}. {question_text(item)}" for n, item in zip(numbers, items))
    response = retry.call("Claude", lambda: client.messages.create(
        model=agent.MODEL, max_tokens=4000, output_config={"effort": "low"}, tools=[ANSWER_TOOL],
        system=("The user replied to numbered questions. Call resolve once for each question the reply clearly "
                "answers. Leave out questions it does not answer or answers unclearly. 'skip' is only for "
                "questions that offer it."),
        messages=[{"role": "user", "content": f"Questions:\n{listing}\n\nThe user's reply:\n{text}"}],
    ))
    answers = {}
    for b in response.content:
        if b.type == "tool_use" and b.input["item"] in numbers:
            answers[b.input["item"]] = b.input["answer"]
    return answers


def answer(client, writer, items, text, answers=None, numbers=None):
    """Apply answers to calendar questions: from a button (answers given), a
    free-text reply or a numbered list (read by the model when not given).

    numbers are the questions' own numbers, which `answers` is keyed by
    (default 1, 2, ...). Returns (reply text, items still unanswered).
    """
    numbers = numbers or list(range(1, len(items) + 1))
    answers = read_answers(client, items, text, numbers) if answers is None else answers
    data, title_rows = load_data()
    linked = linked_rows(data)
    state = load_state()
    edits, inserts, decisions, done, notes = [], [], [], [], []

    for n, item in zip(numbers, items):
        a = answers.get(n)
        if a is None:
            continue
        kind = item["kind"]
        if a == "skip":
            # Not now: no change, asked again after SNOOZE_DAYS.
            until = agent.now().date() + timedelta(days=SNOOZE_DAYS)
            state["snoozed"][item_key(item)] = until.isoformat()
            notes.append(f"{n}: skipped, I'll ask again from {until:%d.%m.%Y}.")
        elif kind == "title" and a in ("yes", "no"):
            decisions.append({"title": item["title"], "track": a == "yes", "by": "you",
                              "reason": f"answered {a} to question {n}"})
        elif kind in ("attend", "cancel") and a in ("yes", "no"):
            row = linked.get(item["event_id"])
            if row is None:
                notes.append(f"{n}: the row for {item.get('entry')} is no longer linked; nothing changed.")
            elif kind == "attend":
                status = "Done" if a == "yes" else "Missed"
                if row_dict(data, row)["Status"] != status:  # already set by hand: nothing to write
                    edits.append(cell_edit(data, row, "Status", status, f"calendar: you answered {a} to \"did you attend\""))
            elif a == "yes":
                outcome = row_dict(data, row)["Outcome"].strip()
                new = "Cancelled" if outcome in ("", "-") else f"{outcome}; Cancelled"
                edits.append(cell_edit(data, row, "Outcome", new, "calendar: event cancelled, you answered yes"))
            else:
                state["ignore_cancel"].append(item["event_id"])
        elif kind in ("match", "same_time") and a in ("yes", "no"):
            if a == "yes":
                row = next((n2 for n2, v in enumerate(data[agent.ACTIVITY]["rows"], 2)
                            if sheet_writer.entry_for(agent.ACTIVITY, row_dict(data, n2)) == item["entry"]), None)
                if row is None:
                    notes.append(f"{n}: row {item['entry']} not found any more; nothing changed.")
                elif agent.is_locked(data, row):
                    # Locked rows are read only: the event is left out instead of linked.
                    state["ignore_event"].append(item["event_id"])
                    notes.append(f"{n}: row {item['entry']} is locked, so the event is left out instead of linked.")
                else:
                    # A multi-day row may already hold another day's event: add to it, never replace.
                    ids = row_event_ids(row_dict(data, row)) + [item["event_id"]]
                    edits.append(cell_edit(data, row, agent.EVENT_ID, ",".join(ids),
                                           f'calendar: matched to event "{item["title"]}", you answered yes'))
            else:
                values = {**item["fields"], agent.SOURCE: SOURCE_VALUE, agent.LOCKED: False,
                          agent.EVENT_ID: item["event_id"]}
                inserts.append({"tab": agent.ACTIVITY, "values": values,
                                "reason": f'calendar: event "{item["title"]}", you answered it is a different event'})
        else:
            notes.append(f"{n}: \"{a}\" is not a possible answer here.")
            continue
        done.append(n)

    edits += title_edits(title_rows, decisions, agent.now().strftime("%d.%m.%Y"))
    logged = writer.apply(edits, inserts) if (edits or inserts) else None
    save_state(state)

    remaining = [item for n, item in zip(numbers, items) if n not in done]
    parts = [f"Answers recorded for question(s) {', '.join(map(str, done))}." if done
             else "I couldn't match your reply to any of the questions."]
    if edits or inserts:
        parts.append(sheet_writer.describe([e for e in edits if e["tab"] == agent.ACTIVITY], inserts, None)
                     + ("\n\n" + sheet_writer.logged_text(logged) if logged else ""))
    if decisions:
        parts.append("Remembered: " + ", ".join(f'"{d["title"]}" = {"yes" if d["track"] else "no"}' for d in decisions))
    parts += notes
    if remaining:
        parts.append("Still open: " + ", ".join(str(n) for n, item in zip(numbers, items) if item in remaining))
    return "\n\n".join(parts), remaining


# ------------------------------------------------------- Saved practice plan

def _event_json(e):
    return {**e, "start": e["start"].isoformat() if e.get("start") else None,
            "end": e["end"].isoformat() if e.get("end") else None}


def _event_back(d):
    return {**d, "start": datetime.fromisoformat(d["start"]) if d["start"] else None,
            "end": datetime.fromisoformat(d["end"]) if d["end"] else None}


def save_plan(plan):
    """Save a practice run's decisions. Rows are saved by their Entry, because
    row numbers can shift before the plan is applied."""
    data = plan["data"]
    entry = lambda row: sheet_writer.entry_for(agent.ACTIVITY, row_dict(data, row))
    PLAN_FILE.write_text(json.dumps({
        "made": agent.now().isoformat(),
        "links": [{"event": _event_json(e), "entry": entry(row), "reason": r} for e, row, r in plan["links"]],
        "adds": [{"event": _event_json(e), "fields": f, "reason": r} for e, f, r in plan["adds"]],
        "moves": [{"event": _event_json(e), "entry": entry(row), "old": o, "new": n} for e, row, o, n in plan["moves"]],
        "titles": plan["titles"],
        "questions": plan["questions"],
    }, ensure_ascii=False, indent=1))


def load_saved_plan(open_keys=()):
    """The saved practice plan, with rows found again by Entry in the Sheet as it is now.

    Returns (plan, notes about anything that could not be placed)."""
    saved = json.loads(PLAN_FILE.read_text())
    if agent.now() - datetime.fromisoformat(saved["made"]) > PLAN_MAX_AGE:
        raise SystemExit(f"The saved plan is older than {PLAN_MAX_AGE}. Run --practice again.")
    data, title_rows = load_data()
    rows_by_entry = {}
    for n in range(2, len(data[agent.ACTIVITY]["rows"]) + 2):
        rows_by_entry.setdefault(sheet_writer.entry_for(agent.ACTIVITY, row_dict(data, n)), []).append(n)
    linked = linked_rows(data)
    plan, notes = new_plan(), []

    def place(item):
        rows = rows_by_entry.get(item["entry"], [])
        if len(rows) != 1:
            notes.append(f"Row {item['entry']!r} is no longer unique or was changed: left out.")
            return None
        return rows[0]

    for item in saved["links"]:
        e = _event_back(item["event"])
        row = place(item)
        if row and e["id"] not in linked:
            plan["links"].append((e, row, item["reason"]))
    for item in saved["moves"]:
        e = _event_back(item["event"])
        row = place(item)
        if row:
            plan["moves"].append((e, row, item["old"], item["new"]))
    for item in saved["adds"]:
        e = _event_back(item["event"])
        if e["id"] in linked:
            notes.append(f"{e['title']!r} is already in the Sheet: not added again.")
        else:
            plan["adds"].append((e, item["fields"], item["reason"]))
    plan["titles"] = saved["titles"]
    plan["questions"] = [q for q in saved["questions"] if item_key(q) not in set(open_keys)]
    plan["data"], plan["titles_rows"] = data, title_rows
    return plan, notes


# ------------------------------------------------------------------ Report

def report(plan):
    """A practice-run report: every decision, nothing written."""
    lines = []
    for e, row, reason in plan["links"]:
        r = row_dict(plan["data"], row)
        lines.append(f"LINK   {cs.date_text(e):<18} {e['title'][:34]:<34} -> row {row} \"{r['Activity'][:40]}\"")
    for e, fields, reason in plan["adds"]:
        lines.append(f"ADD    {fields['Date']:<18} {e['title'][:34]:<34} -> {fields['Category']} | "
                     f"{fields['Activity']} | {fields['Description']}")
    for e, row, old, new in plan["moves"]:
        lines.append(f"MOVE   row {row}: {old} -> {new} ({e['title']})")
    for d in plan["titles"]:
        lines.append(f"TITLE  \"{d['title']}\" -> {'yes' if d['track'] else 'no'} ({d['by']}): {d['reason']}")
    for e, why in plan["skipped"]:
        lines.append(f"SKIP   {cs.date_text(e):<18} {e['title'][:34]:<34} ({why})")
    for e in plan["undecided"]:
        lines.append(f"OPEN   {cs.date_text(e):<18} {e['title'][:34]:<34} (no decision yet; retried next check)")
    lines += [f"NOTE   {n}" for n in plan["notes"]]
    if plan["questions"]:
        lines += ["", "Telegram question message that would be sent:", "-" * 60, question_message(plan["questions"]), "-" * 60]
    if plan.get("later"):
        lines.append(f"({len(plan['later'])} more question(s) wait for a later check.)")
    return "\n".join(lines)


def apply_saved_plan():
    """Write the approved practice plan and send its questions on Telegram."""
    import bot
    import okr
    plan, notes = load_saved_plan(bot.open_calendar_keys())
    writer = sheet_writer.Writer()
    summary = apply(writer, plan)
    if summary:
        okr_edits, logged = okr.recount(writer)
        if okr_edits:
            summary += "\n\n" + okr.describe(okr_edits) + "\n" + sheet_writer.logged_text(logged)
    if notes:
        summary = (summary or "Calendar check.") + "\n\n" + "\n".join(notes)
    if summary:
        bot.send(bot.ALLOWED_CHAT_ID, summary)
        print(summary)
    if plan["questions"]:
        bot.send_calendar_questions(bot.ALLOWED_CHAT_ID, plan["questions"])
        print(f"Sent {len(plan['questions'])} question(s) on Telegram.")
    PLAN_FILE.rename(PLAN_FILE.with_name("calendar_plan.applied.json"))


if __name__ == "__main__":
    if "--apply-plan" in sys.argv:
        apply_saved_plan()
        sys.exit()
    if "--practice" not in sys.argv:
        sys.exit("Run with --practice, or --apply-plan after approving a practice run.")
    plan = check(agent.make_client(), practice=True)
    save_plan(plan)
    print(report(plan))
    print(f"\nWould write: {len(plan['adds'])} new row(s), {len(plan['links'])} link(s), {len(plan['moves'])} "
          f"move(s), {len(plan['titles'])} title decision(s). Would send {len(plan['questions'])} question(s). "
          "Nothing was written or sent.")
    print(f"Decisions saved to {PLAN_FILE.name}. After approval: .venv/bin/python calendar_sync.py --apply-plan")
