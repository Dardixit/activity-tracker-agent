"""The agent: Telegram note -> proposed Sheet changes, or a question for the user.

The agent itself never writes. It returns proposals that have passed the
Sheet rules; bot.py hands them to sheet_writer.py, and dry_run.py only prints them.

When the agent calls ask_user the run pauses. run["pending"] holds the
conversation so far, and continue_run() picks it up with the user's answer.
"""

import json
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import anthropic
import requests
from googleapiclient.discovery import build

import retry
import sheets

# sheets.py has already loaded .env.
MODEL = os.getenv("CLAUDE_MODEL", "").strip() or "claude-haiku-5-5"


def github_identity_token():
    """A fresh GitHub Actions OIDC token for the Claude API. Each one may be
    exchanged only once and expires after about 5 minutes, so one is fetched
    for every exchange instead of being read from a file."""
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=https://api.anthropic.com"
    headers = {"Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"}
    reply = retry.call("GitHub OIDC token", lambda: requests.get(url, headers=headers, timeout=30))
    reply.raise_for_status()
    return reply.json()["value"]


def make_client():
    """The Claude client. On GitHub Actions with the federation variables set
    (ANTHROPIC_FEDERATION_RULE_ID etc.), Workload Identity Federation with a
    fresh GitHub token per exchange: no API key. Elsewhere the usual
    environment credentials (ANTHROPIC_API_KEY when run locally)."""
    if os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL") and os.getenv("ANTHROPIC_FEDERATION_RULE_ID"):
        return anthropic.Anthropic(credentials=anthropic.WorkloadIdentityCredentials(
            identity_token_provider=github_identity_token,
            federation_rule_id=os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
            organization_id=os.environ["ANTHROPIC_ORGANIZATION_ID"],
            service_account_id=os.getenv("ANTHROPIC_SERVICE_ACCOUNT_ID") or None,
            workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID") or None,
        ))
    return anthropic.Anthropic()


ACTIVITY = sheets.ACTIVITY_TAB
PROJECTS = sheets.PROJECTS_TAB
# The Activity Log categories the agent may use (CATEGORIES in .env).
CATEGORIES = sheets.CATEGORIES
# Projects columns the code relies on: the project's name (used in Changes
# entries) and the date it started (counted by okr.py).
PROJECT_NAME = "Project"
PROJECT_STARTED = "Started"

# Columns only the code fills in (setup_sheet.py adds them). The agent never sees
# them as editable: Locked marks read-only Activity Log rows, Source says who
# added a row ("manual" or "agent").
LOCKED = "Locked"
SOURCE = "Source"
EVENT_ID = "Event ID"  # the Google Calendar event a row comes from or was matched to
SYSTEM_COLUMNS = (LOCKED, SOURCE, EVENT_ID)

# Sheet row numbers (row 1 is the header).
# Only setup_sheet.py uses this, to fill the Locked column once. After that,
# locking goes by the Locked column alone, because new Activity Log rows are
# inserted by date and row numbers shift.
ACTIVITY_LOCKED_UNTIL = 171
# Projects rows are only ever appended, so these row numbers stay put.
PROJECTS_LINK_LOCKED = 23    # rows 2-23 of Projects: never change Link

MAX_TURNS = 10

# All dates (the agent's "today", OKR months, Changes timestamps) are Helsinki
# time, whatever timezone the machine running the bot is set to.
TIMEZONE = ZoneInfo("Europe/Helsinki")


def now():
    return datetime.now(TIMEZONE)


# ---------------------------------------------------------------- Sheet data

def read_tabs(service, tabs):
    """Read several tabs in one request (one round trip instead of one per tab)."""
    result = retry.execute(
        service.spreadsheets()
        .values()
        # Quotes around the tab name are required because "Activity Log" has a space.
        .batchGet(spreadsheetId=sheets.SHEET_ID, ranges=[f"'{tab}'" for tab in tabs]),
        "Sheets read",
    )
    # valueRanges come back in the same order as the ranges asked for.
    return [r.get("values", []) for r in result["valueRanges"]]


def load_sheet():
    """Read both tabs once. Returns {tab: {"header": [...], "rows": [...]}}."""
    creds, _ = sheets.load_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    tabs = (ACTIVITY, PROJECTS)
    data = {}
    for tab, rows in zip(tabs, read_tabs(service, tabs)):
        data[tab] = {"header": rows[0], "rows": rows[1:]}
    return data


def used_values(data, column):
    """Distinct non-empty values already used in an Activity Log column."""
    tab = data[ACTIVITY]
    i = tab["header"].index(column)
    return sorted({r[i].strip() for r in tab["rows"] if len(r) > i and r[i].strip()})


def allowed_values(data, column):
    """The values the agent may put in an Activity Log column: CATEGORIES from
    the settings for Category, the values already used in the Sheet for Status."""
    return list(CATEGORIES) if column == "Category" else used_values(data, column)


def row_as_dict(header, row):
    return {h: (row[i] if i < len(row) else "") for i, h in enumerate(header)}


# The first d.m.yyyy / d/m/yyyy in a Date cell, also as a range like "21-22.10.2026".
DATE_KEY_RE = re.compile(r"(\d{1,2})(?:-(\d{1,2}))?[./](\d{1,2})[./](\d{4})")

# Statuses that say something already happened (or didn't).
PAST_STATUSES = ("Done", "Missed")


def event_end(text):
    """The last day of a Date cell as a date (22.10 for "21-22.10.2026"), or None."""
    m = DATE_KEY_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(4)), int(m.group(3)), int(m.group(2) or m.group(1)))
    except ValueError:
        return None


def date_span(text):
    """(first day, last day) of a Date cell as dates: "21-22.10.2026" -> 21.10 and
    22.10, "26.10.2026, 17.00" -> 26.10 twice. None if it has no date."""
    m = DATE_KEY_RE.search(text or "")
    if not m:
        return None
    try:
        year, month = int(m.group(4)), int(m.group(3))
        return date(year, month, int(m.group(1))), date(year, month, int(m.group(2) or m.group(1)))
    except ValueError:
        return None


def past_status_problem(status, date_text):
    """Why Status `status` can't go with this Date, or None if it can.

    Done and Missed say the activity is over, so the row's date (its last day,
    for a range) must be today or earlier in Helsinki.
    """
    if status not in PAST_STATUSES:
        return None
    end, today = event_end(date_text), now().date()
    if end is None:
        return f"Status {status!r} needs a date, but the row's Date is {date_text or 'empty'!r}."
    if end > today:
        return (
            f"Status {status!r} is only allowed for dates up to today ({today:%d.%m.%Y}), "
            f"but the row's date is {date_text!r}, which is in the future."
        )
    return None


def date_key(text):
    """(year, month, day) of a Date cell, for sorting; None if it has no date.
    For a range like "21-22.10.2026" the first day counts."""
    m = DATE_KEY_RE.search(text or "")
    if not m:
        return None
    return int(m.group(4)), int(m.group(3)), int(m.group(1))


def content_columns(data, tab):
    """The columns the agent may fill: everything except the system columns."""
    return [h for h in data[tab]["header"] if h not in SYSTEM_COLUMNS]


def is_locked(data, row):
    """Is this Activity Log row read only? Decided by the Locked column only."""
    tab = data[ACTIVITY]
    if LOCKED not in tab["header"]:
        return True  # no Locked column yet (run setup_sheet.py): treat every row as locked
    i = tab["header"].index(LOCKED)
    values = tab["rows"][row - 2] if 2 <= row < len(tab["rows"]) + 2 else []
    return len(values) > i and values[i].strip().upper() == "TRUE"


def next_free_row(data, tab, proposals=()):
    # +1 for the header, +1 to move past the last data row, then skip rows
    # already claimed by adds proposed earlier in this run.
    claimed = sum(1 for p in proposals if p["action"] == "add" and p["tab"] == tab)
    return len(data[tab]["rows"]) + 2 + claimed


# --------------------------------------------------------------------- Tools

TOOLS = [
    {
        "name": "search_rows",
        "description": (
            f"Search the {ACTIVITY} and {PROJECTS} tabs for rows matching a query. "
            "Matches on any word (names, organisations, projects, events, places). Returns up to 10 rows "
            "per tab with their Sheet row number and whether the row is locked. "
            "Call it more than once with different words if the first search finds nothing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for, e.g. 'Alex library'."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_change",
        "description": (
            "Propose one change to the Sheet. Accepted changes are written after the run and "
            "reported to the user. The change is checked against the Sheet rules and rejected "
            "with an explanation if it breaks one; fix it and call again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "update"]},
                "tab": {"type": "string", "enum": [ACTIVITY, PROJECTS]},
                "row": {
                    "type": "integer",
                    "description": (
                        f"Sheet row number. For update: the existing row. For add to {PROJECTS}: the next "
                        f"free row. For add to {ACTIVITY}: 0 (the row is placed in date order automatically)."
                    ),
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "Column name -> new value. For update, include only the columns that change. "
                        "Column names must match the tab's header exactly."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "reason": {"type": "string", "description": "One or two sentences on why this row and these values."},
                "people": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Required for add: the first name of every person the new row names, e.g. [\"Alex\"]; "
                        "[] if it names no person. Companies, places and events are not people."
                    ),
                },
            },
            "required": ["action", "tab", "row", "fields", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the user a question instead of changing the Sheet, e.g. when the note contradicts "
            "an existing row. Nothing is written. The run pauses; the tool result is the user's answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, quoting the conflicting row(s) by tab and row number.",
                },
                "person": {
                    "type": "string",
                    "description": (
                        "Set (to the first name) when asking whether a person in the note is someone already in "
                        "the Sheet. The user then answers with buttons: Same person, update existing row / "
                        "Same person, new row / Different person."
                    ),
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]

# Words too common to say anything about which row a note is about.
STOP_WORDS = {"the", "for", "and", "with", "at", "on", "in", "to", "of", "a", "an", "my", "me"}


def words(text):
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) >= 2 and w not in STOP_WORDS}


def term_matches(term, row_words):
    """Exact match, or prefix match for words of 4+ letters.

    Prefix matching lets "Northwind" find "Northwinds" and "Kirjasto" find
    "Kirjastossa". Short words (dates like "10", "at") must match exactly.
    """
    if term in row_words:
        return True
    if len(term) < 4:
        return False
    return any(len(w) >= 4 and (w.startswith(term) or term.startswith(w)) for w in row_words)


def search_rows(data, query):
    terms = words(query)
    results = {}
    for tab, t in data.items():
        scored = []
        for n, row in enumerate(t["rows"], start=2):
            row_words = words(" ".join(row))
            score = sum(term_matches(term, row_words) for term in terms)
            if score:
                scored.append((score, n, row))
        scored.sort(key=lambda s: (-s[0], -s[1]))  # best match first, then newest
        hits = []
        for _, n, row in scored[:10]:
            fields = row_as_dict(t["header"], row)
            hit = {"row": n, **{k: v for k, v in fields.items() if k not in SYSTEM_COLUMNS}}
            if tab == ACTIVITY:
                hit["locked"] = is_locked(data, n)
            else:
                hit["link_locked"] = n <= PROJECTS_LINK_LOCKED
            hits.append(hit)
        results[tab] = hits
    return results


# Columns of a new row that are never names, so the name check skips them.
NOT_NAMES = {"Date", "Status", "Category", "Link", "#", PROJECT_STARTED, "Deadline"}
# Ordinary words that start an Activity or Description in capitals.
COMMON_WORDS = {"follow", "up", "with", "met", "meeting", "coffee", "call", "kickoff", "event",
                "session", "message", "chat", "lunch", "talk", "webinar", "workshop", "project"}


def unknown_names(data, fields, texts, existing=None):
    """Capitalised words in new or changed cells that the user never wrote.

    A capitalised word counts as a name unless it is in the user's message (or
    their answers), or is an ordinary word: one the Sheet also uses in lower
    case, a Status/Category word, or one of COMMON_WORDS. Names of people,
    companies and events never appear in lower case, so they must come from the user.

    For an update, `existing` is the row as it is now: a name already in the
    cell being changed is fine (its text is kept), but a name from any other
    cell of the row is not.
    """
    existing = existing or {}
    said = words(" ".join(texts))
    ordinary = set(COMMON_WORDS)
    for tab in data.values():
        for row in tab["rows"]:
            ordinary |= {w for w in re.findall(r"\w+", " ".join(row)) if w[:1].islower()}
    for column in ("Status", "Category"):
        ordinary |= {w.lower() for v in allowed_values(data, column) for w in v.split()}
    found = []
    for column, value in fields.items():
        if column in NOT_NAMES:
            continue
        known = said | words(existing.get(column, ""))
        for w in re.findall(r"\w+", str(value)):
            if w[:1].isupper() and w.lower() not in ordinary and not term_matches(w.lower(), known):
                found.append(w)
    return sorted(set(found))


def check_change(data, change, proposals=(), texts=()):
    """Return a list of rule violations (empty list means the change is allowed).

    `texts` are what the user wrote (the note, and any answers): names in a
    new row must come from there.
    """
    action, tab, row, fields = change["action"], change["tab"], change["row"], change["fields"]
    columns = content_columns(data, tab)
    problems = []

    system = [k for k in fields if k in SYSTEM_COLUMNS]
    if system:
        problems.append(f"{system} are set by the code, not by you. Leave them out.")
    unknown = [k for k in fields if k not in columns and k not in SYSTEM_COLUMNS]
    if unknown:
        problems.append(f"Unknown column(s) for {tab}: {unknown}. Columns are: {columns}.")
    if not fields:
        problems.append("fields is empty.")

    if action == "add":
        # Activity Log rows are inserted by date (sheet_writer.py), so the row
        # number the agent gives there doesn't matter.
        expected = next_free_row(data, tab, proposals)
        if tab == PROJECTS and row != expected:
            problems.append(f"New rows in {tab} go at row {expected}, not {row}.")
        if tab == ACTIVITY:
            missing = [c for c in columns if not fields.get(c, "").strip()]
            if missing:
                problems.append(f"A new {ACTIVITY} row needs every column filled; missing: {missing}.")
    else:  # update
        last = len(data[tab]["rows"]) + 1
        if not 2 <= row <= last:
            problems.append(f"Row {row} does not exist in {tab} (data rows are 2-{last}).")
        elif tab == ACTIVITY and is_locked(data, row):
            problems.append(f"{ACTIVITY} row {row} is locked (read only). Add a new row instead of updating this one.")
        if tab == PROJECTS and row <= PROJECTS_LINK_LOCKED and "Link" in fields:
            problems.append(f"The Link column of {PROJECTS} rows 2-{PROJECTS_LINK_LOCKED} must never change.")

    if tab == ACTIVITY:
        for column in ("Status", "Category"):
            value = fields.get(column)
            allowed = allowed_values(data, column)
            if value is not None and value not in allowed:
                problems.append(f"{column} {value!r} is not allowed. Pick one of: {allowed}.")

        # Done/Missed only for dates up to today. Checked whenever the change
        # sets Status or Date, against the row as it will be afterwards.
        if "Status" in fields or "Date" in fields:
            current = {}
            if action == "update" and 2 <= row < len(data[tab]["rows"]) + 2:
                current = row_as_dict(data[tab]["header"], data[tab]["rows"][row - 2])
            after = {**current, **fields}
            problem = past_status_problem(after.get("Status", ""), after.get("Date", ""))
            if problem:
                problems.append(problem + " Do not propose this. Call ask_user instead: say what the Sheet "
                                "has and ask whether the date is wrong, or whether to note it in Outcome "
                                "and update the status after the date. Do not offer Done/Missed for a future date.")

    existing = {}
    if action == "update" and 2 <= row < len(data[tab]["rows"]) + 2:
        existing = row_as_dict(data[tab]["header"], data[tab]["rows"][row - 2])
    names = unknown_names(data, fields, texts, existing)
    if names:
        what = "The new row" if action == "add" else f"The update of {tab} row {row}"
        problems.append(
            f"{what} mentions {names}, which the user did not write. Never invent people, "
            "companies or events: use only names from the user's message, or call ask_user."
        )

    return problems


# Answers to a same-name question (ask_user with person), as Telegram buttons:
# (value, label, what the agent is told).
PERSON_CHOICES = [
    ("same_update", "Same person, update existing row",
     "add what the note says to the matching existing row and add no new row (if that row is locked, add a "
     "new row instead and leave the locked row as it is)"),
    ("same_new", "Same person, new row",
     "add a new row for the note; it is the same person as in the existing row(s), which stay as they are"),
    ("different", "Different person",
     "add a new row for the note; the existing row(s) are about someone else"),
]
# Words before a capitalised word that make it a person's name ("Coffee with Alex").
PERSON_CUE_RE = re.compile(r"\b(?:with|met|meet|meeting)\s+([A-Z]\w+)")


def first_name(name):
    parts = re.findall(r"\w+", name)
    return parts[0] if parts else ""


def people_in(change):
    """First names of the people a proposed row names: the ones the agent
    listed, plus any capitalised word right after "with"/"met" in a cell."""
    names = {first_name(p) for p in change.get("people") or []}
    for column, value in change["fields"].items():
        if column not in NOT_NAMES:
            names |= set(PERSON_CUE_RE.findall(str(value)))
    return sorted(n for n in names if n)


def rows_naming(data, name):
    """[(tab, row number, short summary)] of every row, open or done, locked or
    not, with `name` as a whole word in one of its cells (case does not matter)."""
    word = name.lower()
    hits = []
    for tab, t in data.items():
        columns = [i for i, h in enumerate(t["header"]) if h not in SYSTEM_COLUMNS]
        for n, row in enumerate(t["rows"], start=2):
            cells = [row[i] for i in columns if i < len(row) and row[i].strip()]
            if word in re.findall(r"\w+", " ".join(cells).lower()):
                hits.append((tab, n, " | ".join(cells)[:120]))
    return hits


def person_question(data, name):
    """The same-name question as the user sees it, written by the code so it
    always matches the buttons: the rows found, then what each button does."""
    hits = rows_naming(data, name)
    lines = [f"Your note names {name}, and the Sheet already has {'a row' if len(hits) == 1 else 'rows'} "
             f"with that name:"]
    locked = []
    for tab, n, text in hits[:10]:
        is_read_only = tab == ACTIVITY and is_locked(data, n)
        if is_read_only:
            locked.append(n)
        lines.append(f"- {tab} row {n}: {text}" + (" (locked)" if is_read_only else ""))
    if len(hits) > 10:
        lines.append(f"- and {len(hits) - 10} more")
    rows = f"row {hits[0][1]}" if len(hits) == 1 else "these rows"
    if len(hits) == 1:
        update = (f"row {hits[0][1]} is locked (read only), so I add a new row instead and leave it as it is."
                  if locked else f"I add what the note says to row {hits[0][1]} and add no new row.")
    else:
        update = ("I add what the note says to the row it is about and add no new row (I ask which one if "
                  "that is unclear)." + (" Locked rows are read only: for those I add a new row instead." if locked else ""))
    lines += ["", f"Is the note about the same {name}?",
              f"- Same person, update existing row: {update}",
              f"- Same person, new row: I add a new row for the note and leave {rows} as {'it is' if len(hits) == 1 else 'they are'}.",
              f"- Different person: I add a new row for the note; {rows} {'is' if len(hits) == 1 else 'are'} about someone else."]
    return "\n".join(lines)


def person_problem(data, change, decided):
    """(first name, why this add must wait for a same-name question), or
    (None, None) if it may go ahead.

    A new row that names a person whose first name is already in any row may
    only be added after the user said, via ask_user with person, whether it is
    the same person. `decided` holds the first names already asked about."""
    if change["action"] != "add":
        return None, None
    if change.get("people") is None:
        return None, ("people is required for add: list the first name of every person the new row names "
                      "([] if none).")
    for name in people_in(change):
        if name.lower() in decided:
            continue
        hits = rows_naming(data, name)
        if hits:
            listing = "\n".join(f"  {tab} row {n}: {text}" for tab, n, text in hits[:10])
            return name, (f"Rows already mention {name!r}:\n{listing}\nDo not add this row yet. Call ask_user with "
                    f"person={name!r}, quoting these rows, and ask whether the note is about the same person. "
                    "The user answers with buttons: " + " / ".join(f'"{label}"' for _, label, _ in PERSON_CHOICES)
                    + ". Propose nothing else in this turn.")
    return None, None


# -------------------------------------------------------------------- Prompt

def system_prompt(data):
    return f"""You keep an activity tracker in a Google Sheet up to date from short Telegram notes the user sends.
For each note, decide whether it updates existing rows or needs new rows, then propose the changes.

The Sheet has two tabs you work with:
- "{ACTIVITY}" columns: {content_columns(data, ACTIVITY)}. One row per activity (meeting, event, outreach, project step, course).
- "{PROJECTS}" columns: {content_columns(data, PROJECTS)}. One row per project.

How to work:
1. Use search_rows to look for rows about the same person, organisation, project or event, in both tabs. Try a couple of
   queries if needed (e.g. the name alone, then name plus place).
2. Check the note against every matching row. If the note contradicts a row (a different date, or a status
   that cannot be true, e.g. an event described as done that the Sheet has in the future), do not propose any
   change: call ask_user, quoting the conflicting row(s) and asking which is right.
3. Otherwise decide. A note continues an existing row only if it is clearly the same thing (same person,
   organisation or project, and the same thread). A new meeting, event or contact is a new {ACTIVITY} row.
   A development in a project usually updates its {PROJECTS} row. If the Sheet already says what the note says,
   propose nothing and say so.
4. Call propose_change for each change, all in the same turn. The run ends as soon as they are all accepted,
   so there is no need for a closing summary. If one is rejected for breaking a rule, fix it and call it again.
   If you need to ask the user anything, ask before proposing: while a question is open, nothing from this
   run is written. Never tell the user something was logged or changed in a question.
5. When ask_user returns the user's answer, apply it: correct the cells the answer says are wrong (unless
   locked) and record whatever is still new in the original note. A cell that already matches the answer is
   not wrong, even if the user asks you to fix its row: leave it. If the answer settles that nothing should
   change, propose nothing. If it is still unclear, ask again.

Rules (propose_change enforces the row rules and allowed values):
- {ACTIVITY} rows marked locked (search_rows shows "locked": true) are read only. If the matching row is
  locked, add a new row instead.
- In {PROJECTS} rows 2-{PROJECTS_LINK_LOCKED}, every column may be updated except Link, which never changes.
- When updating a row, change only the cells the note (or the user's answer) gives new information for. Leave
  every other cell exactly as it is, even if you could word it better or more consistently.
- Never reword or reformat text already in a cell: no rephrasing, no changing date formats, no tidying. If a
  cell already says what the note says, do not touch it. If new information belongs in a cell that already has
  text, keep the existing text exactly and add the new part after it. Replace existing text only when the note
  or answer says it is wrong.
- New {ACTIVITY} rows are placed in date order automatically: give row 0. New {PROJECTS} rows go at the
  next free row, {next_free_row(data, PROJECTS)}; a second new {PROJECTS} row goes one row further down.
- A new {ACTIVITY} row fills all of Date, Status, Category, Activity, Description, Outcome.
- A follow-up mentioned in the note ("follow up next week") becomes its own {ACTIVITY} row, separate from the
  row for the thing that happened: Category "Outreach", Status "Pending", Date = the follow-up date,
  Activity like "Follow up with <name>". "Next week" means the Monday of next week unless the note names a day.
- Status must be one of: {allowed_values(data, "Status")}.
- Category must be one of: {allowed_values(data, "Category")}.
- Write dates as DD.MM.YYYY, like the recent rows. Resolve relative dates ("today", "next week") against today's date given with the note.
- Match the style of existing rows: Activity is a short name (person, organisation or event), Description and
  Outcome are short phrases. Use "-" for an Outcome that is not known yet.
- Before adding a row that names a person, search_rows for their first name alone. If any row mentions that
  first name (open or done, locked or not), do not add the row: call ask_user with person set to the first
  name. The code writes the question the user sees (the rows found and what each button does), so a short
  question is enough. The user answers with buttons ("Same person, update existing row" / "Same person, new
  row" / "Different person"). Then do what the answer says. propose_change rejects such a row until this was asked, and every add must list its people.
- Only use facts from the note and the Sheet. Do not invent surnames, companies or details. Every name of a
  person, company or event you write, in a new row or an update, must be one the user wrote (names already in
  the cell you are updating may stay); propose_change rejects any other name.
- Status "Done" or "Missed" means it already happened (or didn't). Use them only when the row's date is today
  or earlier. If the note says something in the future is done or missed, call ask_user instead. The code
  refuses Done/Missed on a future date, so never offer that as an option. Offer what is possible: correct the
  date if it is wrong, keep the status and note the change in Outcome (e.g. "Won't attend"), or wait and
  update the status after the date.
- Your questions and any closing text are sent to the user as plain-text Telegram messages. Write plain text:
  no markdown, no asterisks for bold, no backticks, no headings. Use short paragraphs, or lines starting with
  "- " for a list."""


# ---------------------------------------------------------------------- Loop

def show(label, value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    print(f"    {label}: " + text.replace("\n", "\n      "))


def run_tool(data, name, args, run):
    """Execute one tool call. Returns (result_text, is_error)."""
    if name == "search_rows":
        return json.dumps(search_rows(data, args["query"]), ensure_ascii=False), False
    if name == "propose_change":
        if run["asked"]:
            return "Rejected: a question is open for the user, so nothing may be proposed until they answer.", True
        problems = check_change(data, args, run["proposals"], run["texts"])
        name, problem = person_problem(data, args, run["decided"])
        if problem:
            problems.append(problem)
            run["needs_person"] = name or run["needs_person"]
        if problems:
            return "Rejected:\n- " + "\n- ".join(problems), True
        run["proposals"].append(args)
        return "Accepted. It will be written after this run.", False
    return f"Unknown tool {name!r}.", True


def to_json(messages):
    """Messages as plain JSON, so a paused run can be saved to a file.

    Assistant turns hold SDK objects; model_dump keeps every field, including
    the thinking-block signatures the API needs to see unchanged.
    """
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            content = [b if isinstance(b, dict) else b.model_dump(mode="json", exclude_none=True) for b in content]
        out.append({"role": m["role"], "content": content})
    return out


def new_run():
    # final_text is the model's closing text when it proposes nothing; problem
    # is set if the run stopped early (refusal, out of tokens, too many turns);
    # pending is the saved conversation when the run paused on a question.
    # texts is everything the user wrote in this conversation (note and answers).
    # decided: {first name: answer} for same-name questions already asked;
    # person: the first name the open question is about (it gets buttons).
    return {"proposals": [], "asked": False, "question": None, "final_text": "",
            "problem": None, "pending": None, "texts": [], "warned_ask": False,
            "decided": {}, "needs_person": None, "person": None}


def start(client, data, note):
    """Run the agent on a new note."""
    today = now().strftime("%A %d.%m.%Y")
    messages = [{"role": "user", "content": f"Today is {today}.\nTelegram note: {note}"}]
    return run_loop(client, data, messages, [note])


def continue_run(client, data, pending, answer, choice=None):
    """Continue a paused run: the user's answer becomes ask_user's tool result.
    `choice` is the button value when a same-name question was answered by a tap."""
    today = now().strftime("%A %d.%m.%Y")
    decided = dict(pending.get("decided", {}))
    content = f"The user answered (today is {today}): {answer}"
    if pending.get("person"):
        decided[pending["person"].lower()] = choice or "answered in words"
        meaning = {value: told for value, _, told in PERSON_CHOICES}.get(choice)
        if meaning:
            content += f"\nThat means: {meaning}."
    results = pending["results"] + [{"type": "tool_result", "tool_use_id": pending["ask_id"], "content": content}]
    messages = pending["messages"] + [{"role": "user", "content": results}]
    return run_loop(client, data, messages, pending.get("texts", []) + [answer], decided)


def run_loop(client, data, messages, texts, decided=None):
    run = new_run()
    run["texts"] = texts
    run["decided"] = dict(decided or {})
    step = 0

    for _ in range(MAX_TURNS):
        response = retry.call("Claude", lambda: client.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "medium"},
            system=system_prompt(data),
            tools=TOOLS,
            messages=messages,
        ))

        if response.stop_reason == "refusal":
            run["problem"] = f"The model declined this request ({response.stop_details})."
            print(f"  {run['problem']}")
            break
        if response.stop_reason == "max_tokens":
            run["problem"] = "The model ran out of tokens before finishing."
            print(f"  {run['problem']}")
            break

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"  Model: {block.text.strip()}")

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            run["final_text"] = "\n".join(
                b.text.strip() for b in response.content if b.type == "text" and b.text.strip()
            )
            break

        messages.append({"role": "assistant", "content": response.content})
        results = []
        ask_id = None
        for call in tool_uses:
            step += 1
            print(f"\n  Step {step}: {call.name}")
            show("input", call.input)
            if call.name == "ask_user" and ask_id is None and run["proposals"] and not run["warned_ask"]:
                # A question discards what was already accepted (nothing is
                # written while a question is open). Make the agent choose,
                # so its question can't claim those changes were made.
                run["warned_ask"] = True
                n = len(run["proposals"])
                text, is_error = (
                    f"Not asked yet. You already proposed {n} change(s) in this run, and asking a question "
                    "means NOTHING from this run is written, those changes included. Either finish without "
                    "the question (the accepted changes are then written), or call ask_user again; if you "
                    "do, the question must not say anything was logged or changed.", True)
                show("result", text)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})
                continue
            if call.name == "ask_user" and ask_id is None:
                # No tool result yet: the user's answer will be the result.
                ask_id = call.id
                run["asked"] = True
                run["question"] = call.input["question"]
                # A question after a same-name rejection is that question, even
                # if the agent left person out: its answer must unblock the name.
                run["person"] = first_name(call.input.get("person") or "") or run["needs_person"]
                if run["person"] and rows_naming(data, run["person"]):
                    # The user sees the code's wording, which matches the buttons.
                    run["question"] = person_question(data, run["person"])
                print("    (paused: question for the user)")
                continue
            if call.name == "ask_user":
                text, is_error = "Only one question at a time. Ask this after the first is answered.", True
            else:
                text, is_error = run_tool(data, call.name, call.input, run)
            if call.name == "search_rows":
                found = json.loads(text)
                summary = {tab: [f"row {h['row']}: " + " | ".join(
                    str(v) for k, v in h.items() if k not in ("row", "locked", "link_locked") and v
                )[:110] for h in hits] for tab, hits in found.items()}
                show("found", summary)
            else:
                show("result", text)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})

        if ask_id:
            # Proposals made in the same turn as a question are dropped: nothing
            # is written while a question is open.
            run["proposals"] = []
            run["pending"] = {"messages": to_json(messages), "results": results, "ask_id": ask_id,
                              "texts": texts, "decided": run["decided"], "person": run["person"]}
            break
        messages.append({"role": "user", "content": results})
        # A turn of nothing but accepted proposals means the agent is done. The
        # reply is built from the proposals, so a closing summary call would
        # only add time.
        if all(c.name == "propose_change" for c in tool_uses) and not any(r["is_error"] for r in results):
            break
    else:
        run["problem"] = f"The agent did not finish within {MAX_TURNS} turns."

    return run


def format_proposal(data, p):
    """One proposed change as plain text, with before -> after for updates."""
    where = "(placed by date)" if p["action"] == "add" and p["tab"] == ACTIVITY else f"row {p['row']}"
    lines = [f"{p['action'].upper()} {p['tab']} {where}"]
    header = data[p["tab"]]["header"]
    current = {}
    if p["action"] == "update":
        current = row_as_dict(header, data[p["tab"]]["rows"][p["row"] - 2])
    for column in header:
        if column in p["fields"]:
            if p["action"] == "update":
                lines.append(f"  {column}: {current.get(column, '') or '(empty)'} -> {p['fields'][column]}")
            else:
                lines.append(f"  {column}: {p['fields'][column]}")
    lines.append(f"Reason: {p['reason']}")
    return "\n".join(lines)


def format_reply(data, run):
    """A dry-run result as plain text (dry_run.py; the bot reports real writes itself)."""
    if run["asked"]:
        buttons = " / ".join(f"[{label}]" for _, label, _ in PERSON_CHOICES) if run["person"] else ""
        return "Question before I change anything:\n\n" + run["question"] + (f"\n\n{buttons}" if buttons else "")
    if run["problem"]:
        return "Something went wrong: " + run["problem"]
    if not run["proposals"]:
        return "No change proposed.\n\n" + (run["final_text"] or "The Sheet already covers this.")
    parts = ["Proposed change (dry run, nothing written):"]
    parts += [format_proposal(data, p) for p in run["proposals"]]
    return "\n\n".join(parts)
