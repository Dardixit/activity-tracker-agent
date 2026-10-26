"""A question answered once is never sent again: every calendar question kind,
every answer, offline.

For each case a calendar check asks the question, it is answered through
calendar_sync.answer (the code a button tap runs), and the next check, on the
same calendar events, must not ask it again. The fake model is the worst
case: every time it sees an event it tries to ask about it again. Skip is the
one planned exception: the question comes back after SNOOZE_DAYS, which is
checked too.

Nothing touches the network: the Sheet is a small in-memory one, the writer
applies changes to it, and calendar state goes to a temporary file.

Run with:  .venv/bin/python test_answers.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["STATE_BACKEND"] = "files"
import agent  # noqa: E402
import calendar_sync as sync  # noqa: E402
import sheet_writer  # noqa: E402

HEADER = ["Date", "Status", "Category", "Activity", "Description", "Outcome", "Locked", "Source", "Event ID"]
NOW = agent.now().replace(minute=0, second=0, microsecond=0)
results = []


def expect(ok, text):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {text}")


def event(eid, title, start, hours=1, cancelled=False):
    return {"id": eid, "title": title, "start": start, "end": start + timedelta(hours=hours), "all_day": False,
            "cancelled": cancelled, "declined": False, "location": "", "description": "", "recurring_id": None}


def row(date, activity, status="Confirmed", event_id="", locked=False, description="-"):
    return [date, status, "Networking", activity, description, "-", "TRUE" if locked else "FALSE", "agent", event_id]


def when(dt):
    return f"{dt:%d.%m.%Y}, {dt:%H.%M}"


class Sheet:
    """The Activity Log and Calendar titles tabs, changed by FakeWriter."""

    def __init__(self, rows):
        self.data = {agent.ACTIVITY: {"header": HEADER, "rows": [list(r) for r in rows]},
                     agent.PROJECTS: {"header": ["#", agent.PROJECT_NAME, "Link"], "rows": []}}
        self.titles = [list(sync.TITLES_HEADER)]


class FakeWriter:
    """Applies edits and inserts to the in-memory Sheet; refuses what the real
    writer refuses for locked rows, plus anything `refuse` matches."""

    def __init__(self, sheet, refuse=lambda item: None):
        self.sheet, self.refuse = sheet, refuse

    def apply_allowed(self, edits, inserts):
        refused = []
        tab = self.sheet.data[agent.ACTIVITY]
        for e in edits:
            why = self.refuse(e) or (e["tab"] == agent.ACTIVITY and agent.is_locked(self.sheet.data, e["row"])
                                     and f"row {e['row']} is locked")
            if why:
                refused.append(([e], why))
            elif e["tab"] == sync.TITLES_TAB:
                while len(self.sheet.titles) < e["row"]:
                    self.sheet.titles.append([""] * len(sync.TITLES_HEADER))
                line = self.sheet.titles[e["row"] - 1]
                line += [""] * (len(sync.TITLES_HEADER) - len(line))
                line[e["col"]] = e["new"]
            else:
                tab["rows"][e["row"] - 2][e["col"]] = e["new"]
        for i in inserts:
            why = self.refuse(i)
            if why:
                refused.append(([i], why))
                continue
            values = {**i["values"]}
            tab["rows"].append([str(values.get(c, "")) if not isinstance(values.get(c), bool)
                                else ("TRUE" if values[c] else "FALSE") for c in HEADER])
            i["row"] = len(tab["rows"]) + 1  # the real writer sets this too
        return ((2, 3) if len(refused) < len(edits) + len(inserts) else None), refused


def worst_model(script):
    """A Batch.run that, for every event it is shown, calls the tool `script` names."""
    def run(self, client):
        for eid, e in list(self.ids.items()):
            if e["id"] in script:
                name, args = script[e["id"]](self)
                self.run_tool(name, {"event": eid, **args})
    return run


def check(sheet, events, script):
    sync.Batch.run = worst_model(script)
    return sync.check(object(), events=events, data=sheet.data, title_rows=sheet.titles,
                      get_event=lambda eid: (_ for _ in ()).throw(AssertionError(f"get_event({eid})")))


def keys(plan):
    return [sync.item_key(q) for q in plan["questions"]]


def case(name, rows, events, script, kind, answer, refuse=lambda item: None):
    """Ask, answer, check again. Returns the sheet for extra checks."""
    sync.STATE_FILE = Path(tempfile.mkdtemp()) / "calendar_state.json"
    sheet = Sheet(rows)
    sync.load_data = lambda: (sheet.data, sheet.titles)
    plan = check(sheet, events, script)
    asked = [q for q in plan["questions"] if q["kind"] == kind]
    if not asked:
        expect(False, f"{name}: the first check asks a {kind} question (asked: {keys(plan)})")
        return sheet
    q = asked[0]
    sync.answer(None, FakeWriter(sheet, refuse), [q], "", answers={1: answer}, numbers=[1])
    again = check(sheet, events, script)
    repeat = [k for k in keys(again) if k == sync.item_key(q)]
    if answer == "skip":
        expect(not repeat, f"{name}: skip -> not asked at the next check")
        later = NOW + timedelta(days=sync.SNOOZE_DAYS, hours=1)
        real_now, agent.now = agent.now, lambda: later
        try:
            back = check(sheet, events, script)
        finally:
            agent.now = real_now
        expect(sync.item_key(q) in keys(back),
               f"{name}: skip -> asked again after {sync.SNOOZE_DAYS} days (planned)")
    else:
        expect(not repeat, f"{name}: {answer} -> never asked again (next check asks: {keys(again) or 'nothing'})")
    return sheet


def main():
    tomorrow = NOW + timedelta(days=1, hours=2)
    earlier = NOW - timedelta(hours=3)

    # Title: an unlinked event the model is unsure about.
    for a in ("yes", "no", "skip"):
        case(f"title {a}", [], [event("T1", "Mystery meetup", tomorrow)],
             {"T1": lambda b: ("ask_title", {"title": "Mystery meetup"})}, "title", a)

    # Match: a row on another day might be the event.
    for a in ("yes", "no", "skip"):
        rows = [row(when(tomorrow + timedelta(days=1)), "Coffee with Alex")]
        case(f"match {a}", rows, [event("M1", "Coffee Alex", tomorrow)],
             {"M1": lambda b: ("ask_match", {"row": 2, "category": "Networking", "activity": "Coffee Alex",
                                             "description": "-"})}, "match", a)

    # Same start time as a row linked to another event.
    for a in ("yes", "no", "skip"):
        rows = [row(when(tomorrow), "Workshop at Hub42", event_id="S0")]
        evs = [event("S0", "Workshop at Hub42", tomorrow), event("S1", "Hub42 workshop invite", tomorrow)]
        case(f"same_time {a}", rows, evs,
             {"S1": lambda b: ("add_row", {"category": "Networking", "activity": "Hub42 workshop invite",
                                           "description": "-", "reason": "test"})}, "same_time", a)

    # Duplicate: an unlinked same-day row about the same person.
    for a in ("yes", "no", "skip"):
        rows = [row(f"{tomorrow:%d.%m.%Y}", "Call with Robin")]
        evs = [event("D1", "Robin catch-up", tomorrow)]
        case(f"duplicate {a}", rows, evs,
             {"D1": lambda b: ("add_row", {"category": "Networking", "activity": "Robin catch-up",
                                           "description": "-", "reason": "test"})}, "duplicate", a)

    # Did you attend: a linked row whose event has ended.
    for a in ("yes", "no", "skip"):
        case(f"attend {a}", [row(when(earlier), "Language café", event_id="A1")],
             [event("A1", "Language café", earlier)], {}, "attend", a)

    # Cancelled in the calendar.
    for a in ("yes", "no", "skip"):
        case(f"cancel {a}", [row(when(tomorrow), "Plan with Sam", event_id="C1")],
             [event("C1", "Plan with Sam", tomorrow, cancelled=True)], {}, "cancel", a)

    # An answer whose write a rule refuses (here: the row is locked).
    case("attend yes, row locked", [row(when(earlier), "Old meetup", event_id="L1", locked=True)],
         [event("L1", "Old meetup", earlier)], {}, "attend", "yes")
    case("cancel yes, row locked", [row(when(tomorrow), "Old plan", event_id="L2", locked=True)],
         [event("L2", "Old plan", tomorrow, cancelled=True)], {}, "cancel", "yes")

    # A change a rule refused: "Try again next check" / "Leave this event out".
    for a in ("yes", "no"):
        sync.STATE_FILE = Path(tempfile.mkdtemp()) / "calendar_state.json"
        moved = tomorrow + timedelta(days=2)
        sheet = Sheet([row(when(tomorrow), "Mentor call", event_id="R1")])
        sync.load_data = lambda: (sheet.data, sheet.titles)
        refuse = lambda item: "test rule: no moves" if item.get("column") == "Date" else None
        writer = FakeWriter(sheet, refuse)
        evs = [event("R1", "Mentor call", moved)]
        plan = check(sheet, evs, {})
        sync.apply(writer, plan)
        asked = [q for q in plan["questions"] if q["kind"] == "refused"]
        if not asked:
            expect(False, f"refused {a}: the refused move is asked about")
            continue
        sync.answer(None, writer, asked[:1], "", answers={1: a}, numbers=[1])
        again = check(sheet, evs, {})
        sync.apply(writer, again)
        repeat = [q for q in again["questions"] if sync.item_key(q) == sync.item_key(asked[0])]
        expect(not repeat, f"refused {a}: never asked again (next check asks: {keys(again) or 'nothing'})")

    # Your own names don't make a duplicate.
    agent.OWN_NAMES = {"morgan", "lee"}  # as if OWNER_NAMES=Morgan,Lee
    sheet = Sheet([row(f"{tomorrow:%d.%m.%Y}", "Call with Morgan Lee")])
    expect(not agent.twin_rows(sheet.data, {"Date": when(tomorrow), "Activity": "Morgan Lee for Product Lead",
                                            "Description": "Final round"}),
           "own names (OWNER_NAMES) don't make a duplicate")
    expect(agent.twin_rows(sheet.data | {agent.ACTIVITY: {"header": HEADER, "rows": [
        row(f"{tomorrow:%d.%m.%Y}", "Call with Robin")]}}, {"Date": when(tomorrow), "Activity": "Robin catch-up",
                                                        "Description": "-"}),
           "another person's name still makes a duplicate")

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
