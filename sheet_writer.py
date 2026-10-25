"""Write changes to the Sheet, logging every changed cell in the Changes tab.

Each write is one spreadsheets.batchUpdate request that holds both the cell
changes and their Changes rows. Google applies a batchUpdate all-or-nothing,
so a cell is never changed without its log row, or logged without being changed.

Before writing, every cell is re-read: if it no longer holds the value the
agent saw (you edited it by hand meanwhile), nothing is written.

Two kinds of change:
- An edit changes one cell of an existing row (or of a new Projects row,
  which is appended at the bottom):
    {"tab", "row", "col" (0-based index), "column" (name for the log),
     "old" (value the agent saw), "new", "reason", "action" ("add"/"update"), "entry"}
- An insert adds a whole Activity Log row at its place in date order:
    {"tab", "values" ({column: value}), "reason"}; apply() fills in "row".
  The row is a real inserted row, so it takes the formatting and dropdowns of
  the row above it.

When an edit changes an Activity Log row's Date, the row is also moved to
its new place in date order, in the same request (it keeps its own formatting
and dropdowns), and the move is logged as "(whole row)".

Because inserts and moves shift row numbers, every Changes row also names the
entry ("date | activity" for the Activity Log, "#n project" for Projects).
"""

import sys

from googleapiclient.discovery import build

import agent
import retry
import sheets

CHANGES = "Changes"
CHANGES_HEADER = ["Timestamp", "Tab", "Row", "Entry", "Column", "Old value", "New value", "Reason", "Error"]
CHECKBOX = {"condition": {"type": "BOOLEAN"}}


class WriteUnknown(Exception):
    """A write failed in a way that leaves open whether it reached the Sheet."""


class WriteRefused(Exception):
    """Raised before anything is sent: nothing was written."""


def cell(value):
    """A Python value as Sheets CellData. Booleans become checkboxes."""
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}, "dataValidation": CHECKBOX}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def shown(value):
    """How a value reads back from the Sheet as text (TRUE/FALSE for checkboxes)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def update_cells(sheet_id, row_index, col_index, rows, fields="userEnteredValue"):
    """An updateCells request writing `rows` (lists of CellData) from one corner.

    By default only the value is written, so the cell keeps its dropdown and
    formatting. Pass fields="userEnteredValue,dataValidation" to also set the
    validation (checkboxes).
    """
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row_index, "columnIndex": col_index},
            "rows": [{"values": r} for r in rows],
            "fields": fields,
        }
    }


def log_row(tab, row, entry, column, old, new, reason):
    timestamp = agent.now().strftime("%d.%m.%Y %H:%M:%S")  # Helsinki time
    return [timestamp, tab, row, entry, column, shown(old), shown(new), reason, False]


def entry_for(tab, row):
    """What a row is about, for the Changes log: row numbers shift, this doesn't."""
    if tab == agent.ACTIVITY:
        return f"{shown(row.get('Date', ''))} | {shown(row.get('Activity', ''))}"
    if tab == agent.PROJECTS:
        return f"#{shown(row.get('#', ''))} {shown(row.get(agent.PROJECT_NAME, '')).strip()}"
    if "Title" in row:  # the Calendar titles tab
        return shown(row["Title"])
    return ""


def insert_position(rows, key):
    """Sheet row number where a row dated `key` goes, given tab rows including the header.

    It goes before the first row with a later date or no date, so rows with the
    same date keep the order they were added in, and undated rows stay at the end.
    A row with no date goes at the very end.
    """
    if key is not None:
        for n, values in enumerate(rows[1:], start=2):
            k = agent.date_key(values[0] if values else "")
            if k is None or k > key:
                return n
    return len(rows) + 1


def insert_row_requests(sheet_id, row, cells):
    """Requests that insert a row at Sheet row `row` and fill it with `cells`.

    The new row copies formatting and data validation (the Status and Category
    dropdowns, the Locked checkbox) from the row above, or from the row below
    when it goes right under the header.
    """
    index = row - 1
    from_above = index - 1 >= 1  # the row above is a data row, not the header
    source = index - 1 if from_above else index + 1

    def whole_row(i):
        return {"sheetId": sheet_id, "startRowIndex": i, "endRowIndex": i + 1}

    return [
        {"insertDimension": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": index, "endIndex": index + 1},
            "inheritFromBefore": from_above,
        }},
        {"copyPaste": {"source": whole_row(source), "destination": whole_row(index), "pasteType": "PASTE_FORMAT"}},
        {"copyPaste": {"source": whole_row(source), "destination": whole_row(index),
                       "pasteType": "PASTE_DATA_VALIDATION"}},
        # Values only, so the copied dropdowns and checkbox stay.
        update_cells(sheet_id, index, 0, [cells]),
    ]


def move_row_request(sheet_id, source_index, destination_index):
    """Move one row (0-based index) so it ends up before row `destination_index`
    (0-based, counted before the move)."""
    return {"moveDimension": {
        "source": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": source_index, "endIndex": source_index + 1},
        "destinationIndex": destination_index,
    }}


def reposition(rows, sheet_id, targets):
    """Move rows to their place in date order.

    rows: the tab's rows (header included) as they will be after this request's
    edits; changed in place. targets: [(row list object, reason)], found by
    identity, since row numbers change as rows move.
    Returns (requests, logs, {id(row object): final Sheet row}).
    """
    requests, logs, final = [], [], {}
    for obj, reason in targets:
        s = next(i for i, r in enumerate(rows) if r is obj)
        without = rows[:s] + rows[s + 1:]
        p0 = insert_position(without, agent.date_key(obj[0] if obj else "")) - 1
        final[id(obj)] = p0 + 1
        if p0 == s:
            continue  # already in place
        requests.append(move_row_request(sheet_id, s, p0 if p0 < s else p0 + 1))
        rows.pop(s)
        rows.insert(p0, obj)
        entry = entry_for(agent.ACTIVITY, agent.row_as_dict(rows[0], obj))
        logs.append(log_row(agent.ACTIVITY, p0 + 1, entry, "(whole row)", f"row {s + 1}", f"row {p0 + 1}",
                            f"{reason} (moved to keep date order)"))
    return requests, logs, final


def date_order(insert):
    key = agent.date_key(shown(insert["values"].get("Date", "")))
    return (key is None, key or ())


class Writer:
    def __init__(self):
        if not sheets.SHEET_ID:
            sys.exit("No Sheet ID to write to. Set GOOGLE_TEST_SHEET_ID in .env.")
        # SHEET_TARGET=real is the only way to write to the real Sheet. This
        # also catches GOOGLE_TEST_SHEET_ID being set to the real Sheet's ID.
        if sheets.SHEET_ID == sheets.REAL_SHEET_ID and sheets.SHEET_TARGET != "real":
            sys.exit("Refusing to write: the target is the real Sheet but SHEET_TARGET is not 'real'.")
        creds, _ = sheets.load_credentials(write=True)
        self.service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        # Put in front of every logged reason. test_bot.py sets it to "test: "
        # so test writes are easy to tell apart in the Changes tab.
        self.reason_prefix = ""
        self.refresh_ids()

    def refresh_ids(self):
        meta = retry.execute(
            self.service.spreadsheets().get(spreadsheetId=sheets.SHEET_ID, fields="sheets.properties(title,sheetId)"),
            "Sheets read",
        )
        self.sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    def read(self, tabs):
        """{tab: rows including the header}, read in one request."""
        tabs = list(tabs)
        result = retry.execute(
            self.service.spreadsheets().values().batchGet(spreadsheetId=sheets.SHEET_ID, ranges=[f"'{t}'" for t in tabs]),
            "Sheets read",
        )
        return {t: r.get("values", []) for t, r in zip(tabs, result["valueRanges"])}

    def commit(self, requests, log_rows, changes_sheet_id=None, changes_rows_before=None):
        """Send `requests` plus the Changes rows in one atomic batchUpdate.

        Returns the (first, last) Changes row numbers the log went to.
        setup_sheet.py passes changes_sheet_id when the Changes tab is created
        in this same request.
        """
        if changes_sheet_id is None:
            changes_sheet_id = self.sheet_ids[CHANGES]
        if changes_rows_before is None:
            changes_rows_before = len(self.read([CHANGES])[CHANGES])
        if self.reason_prefix:
            reason = CHANGES_HEADER.index("Reason")
            log_rows = [r[:reason] + [self.reason_prefix + r[reason]] + r[reason + 1:] for r in log_rows]
        requests = requests + [{
            "appendCells": {
                "sheetId": changes_sheet_id,
                "rows": [{"values": [cell(v) for v in r]} for r in log_rows],
                "fields": "userEnteredValue,dataValidation",
            }
        }]
        request = self.service.spreadsheets().batchUpdate(spreadsheetId=sheets.SHEET_ID, body={"requests": requests})
        retry.call("Sheets write", lambda: self.send_once(request, changes_rows_before, len(log_rows)))
        return changes_rows_before + 1, changes_rows_before + len(log_rows)

    def send_once(self, request, changes_rows_before, logged):
        """Send a batchUpdate once. After a network error the request may still
        have reached Google, so sending it again could write it twice. The
        request is atomic and appends to Changes, so the Changes row count says
        which: the error is raised again (to be retried) only if nothing landed."""
        try:
            request.execute()
        except Exception as err:
            if not retry.is_network_error(err):
                raise
            try:
                rows = len(self.read([CHANGES])[CHANGES])
            except Exception:
                rows = None
            if rows == changes_rows_before + logged:
                print(f"Sheets write: {retry.describe(err)}, but the write went through.")
                return
            if rows != changes_rows_before:
                raise WriteUnknown(f"the write failed ({retry.describe(err)}) and the Changes tab does not show "
                                   "whether it went through") from err
            raise

    def apply(self, edits, inserts=()):
        """Check every change against the Sheet as it is now, then write them all.

        Edits go first (their row numbers are from before any insert), then the
        inserts in date order. Raises WriteRefused (nothing written) if a cell
        changed since the agent read it, or an edit would touch a locked row or
        a protected Link. Returns the (first, last) Changes rows of the log.
        """
        if not edits and not inserts:
            return None
        fresh = self.read({e["tab"] for e in edits} | {i["tab"] for i in inserts} | {CHANGES})
        if fresh[CHANGES][:1] != [CHANGES_HEADER]:
            raise WriteRefused(f"The {CHANGES} tab has an old layout. Run setup_sheet.py first.")

        for e in edits:
            rows = fresh[e["tab"]]
            header = rows[0] if rows else []
            values = rows[e["row"] - 1] if e["row"] - 1 < len(rows) else []
            current = values[e["col"]] if e["col"] < len(values) else ""
            where = f"{e['tab']} row {e['row']}, {e['column']}"

            if e["row"] > 1 and e["col"] < len(header) and header[e["col"]] != e["column"]:
                raise WriteRefused(f"{where}: the columns have moved since I read the Sheet.")
            if current != shown(e["old"]):
                raise WriteRefused(
                    f"{where} changed since I read it (was {shown(e['old'])!r}, now {current!r})."
                )
            # Same rules as agent.check_change, checked again on fresh data.
            if e["tab"] == agent.ACTIVITY and agent.LOCKED in header:
                i = header.index(agent.LOCKED)
                if len(values) > i and values[i].strip().upper() == "TRUE":
                    raise WriteRefused(f"{where}: row {e['row']} is locked.")
            if (e["tab"] == agent.PROJECTS and e["column"] == "Link"
                    and 2 <= e["row"] <= agent.PROJECTS_LINK_LOCKED):
                raise WriteRefused(f"{where}: Link never changes in rows 2-{agent.PROJECTS_LINK_LOCKED}.")

        # Done/Missed only for dates up to today (Helsinki), checked on each
        # Activity Log row as it will read after the change.
        touched = {}
        for e in edits:
            if e["tab"] == agent.ACTIVITY and e["column"] in ("Status", "Date"):
                touched.setdefault(e["row"], {})[e["column"]] = shown(e["new"])
        for row, changed in touched.items():
            rows = fresh[agent.ACTIVITY]
            after = {**agent.row_as_dict(rows[0], rows[row - 1] if row - 1 < len(rows) else []), **changed}
            problem = agent.past_status_problem(after["Status"], after["Date"])
            if problem:
                raise WriteRefused(f"{agent.ACTIVITY} row {row}: {problem}")
        for ins in inserts:
            v = ins["values"]
            problem = agent.past_status_problem(shown(v.get("Status", "")), shown(v.get("Date", "")))
            if problem:
                raise WriteRefused(f"New {ins['tab']} row: {problem}")

        requests, logs = [], []
        for e in edits:
            # Checkboxes need their validation set; any other value is written
            # alone, so the cell keeps its dropdown.
            fields = "userEnteredValue,dataValidation" if isinstance(e["new"], bool) else "userEnteredValue"
            requests.append(update_cells(self.sheet_ids[e["tab"]], e["row"] - 1, e["col"], [[cell(e["new"])]], fields))
            logs.append(log_row(e["tab"], e["row"], e.get("entry", ""), e["column"], e["old"], e["new"], e["reason"]))

        # The tabs as they will read after the edits above.
        simulated = {tab: [list(r) for r in rows] for tab, rows in fresh.items()}
        for e in edits:
            rows = simulated[e["tab"]]
            while len(rows) < e["row"]:
                rows.append([])
            r = rows[e["row"] - 1]
            r += [""] * (e["col"] + 1 - len(r))
            r[e["col"]] = shown(e["new"])

        # Activity Log rows whose Date changed move to their new place in date order.
        moved = {}  # Sheet row before -> its row list object, for rows whose Date changed
        if agent.ACTIVITY in simulated:
            targets = []
            for e in edits:
                if e["tab"] == agent.ACTIVITY and e["column"] == "Date" and e["row"] not in moved:
                    moved[e["row"]] = simulated[agent.ACTIVITY][e["row"] - 1]
                    targets.append((moved[e["row"]], e["reason"]))
            moves, move_logs, _ = reposition(simulated[agent.ACTIVITY], self.sheet_ids[agent.ACTIVITY], targets)
            requests += moves
            logs += move_logs

        # Inserts in date order: each one then lands after the ones before it,
        # so the row number worked out here is also its final row number.
        for ins in sorted(inserts, key=date_order):
            rows = simulated[ins["tab"]]
            header = rows[0]
            key = agent.date_key(shown(ins["values"].get("Date", "")))
            ins["row"] = row = insert_position(rows, key)
            requests += insert_row_requests(
                self.sheet_ids[ins["tab"]], row, [cell(ins["values"].get(h, "")) for h in header]
            )
            rows.insert(row - 1, [shown(ins["values"].get(h, "")) for h in header])
            entry = entry_for(ins["tab"], ins["values"])
            for column in header:
                if column in ins["values"]:
                    logs.append(log_row(ins["tab"], row, entry, column, "", ins["values"][column], ins["reason"]))

        # Where moved rows end up, after this request's inserts too (for the reply).
        for e in edits:
            obj = moved.get(e["row"]) if e["tab"] == agent.ACTIVITY else None
            if obj is not None:
                final = next(i for i, r in enumerate(simulated[agent.ACTIVITY]) if r is obj) + 1
                if final != e["row"]:
                    e["moved_to"] = final

        return self.commit(requests, logs, changes_rows_before=len(fresh[CHANGES]))

    def place_rows(self, row_numbers, reason):
        """Move these Activity Log rows to their place in date order (a repair).
        Returns the logged Changes rows, or None if they were already in place."""
        fresh = self.read([agent.ACTIVITY, CHANGES])
        rows = [list(r) for r in fresh[agent.ACTIVITY]]
        targets = [(rows[n - 1], reason) for n in row_numbers]
        requests, logs, _ = reposition(rows, self.sheet_ids[agent.ACTIVITY], targets)
        if not requests:
            return None
        return self.commit(requests, logs, changes_rows_before=len(fresh[CHANGES]))


def edits_from_proposals(data, proposals):
    """Turn the agent's accepted proposals into (edits, inserts).

    `data` is the Sheet as the agent saw it, so each edit's "old" is what the
    agent based its decision on; Writer.apply checks it still holds.
    New Activity Log rows become inserts (placed by date); new Projects
    rows are appended, one edit per cell.
    """
    edits, inserts = [], []
    for p in proposals:
        tab, row = p["tab"], p["row"]
        header = data[tab]["header"]
        fields = dict(p["fields"])
        if p["action"] == "add":
            if agent.SOURCE in header:
                fields[agent.SOURCE] = "agent"
            if tab == agent.ACTIVITY:
                if agent.LOCKED in header:
                    fields[agent.LOCKED] = False
                inserts.append({"tab": tab, "values": fields, "reason": p["reason"]})
                continue
        current = {} if p["action"] == "add" else agent.row_as_dict(header, data[tab]["rows"][row - 2])
        entry = entry_for(tab, {**current, **fields})  # the row as it will read after the change
        for column, value in fields.items():
            old = current.get(column, "")
            if column == "#" and str(value).isdigit():
                value = int(value)  # the # column holds numbers
            if p["action"] == "update" and shown(value) == old:
                continue  # no change
            edits.append({
                "tab": tab, "row": row, "col": header.index(column), "column": column,
                "old": old, "new": value, "reason": p["reason"], "action": p["action"], "entry": entry,
            })
    return edits, inserts


def describe(edits, inserts, logged):
    """Plain-text summary of written changes, grouped by row, for the Telegram reply."""
    parts = []
    for ins in inserts:
        lines = [f"Added {ins['tab']} row {ins['row']} (in date order):"]
        lines += [f"  {c}: {shown(v)}" for c, v in ins["values"].items() if c not in agent.SYSTEM_COLUMNS]
        lines.append(f"Reason: {ins['reason']}")
        parts.append("\n".join(lines))
    groups = {}
    for e in edits:
        groups.setdefault((e["tab"], e["row"]), []).append(e)
    for (tab, row), group in groups.items():
        shown_edits = [e for e in group if e["column"] not in agent.SYSTEM_COLUMNS]
        if group[0]["action"] == "add":
            lines = [f"Added {tab} row {row}:"]
            lines += [f"  {e['column']}: {shown(e['new'])}" for e in shown_edits]
        else:
            moved = group[0].get("moved_to")
            lines = [f"Updated {tab} row {row}" + (f" (now row {moved}, in date order)" if moved else "") + ":"]
            lines += [f"  {e['column']}: {shown(e['old']) or '(empty)'} -> {shown(e['new'])}" for e in shown_edits]
        lines.append(f"Reason: {group[0]['reason']}")
        parts.append("\n".join(lines))
    if logged:
        parts.append(logged_text(logged))
    return "\n\n".join(parts)


def logged_text(logged):
    first, last = logged
    return f"Logged in {CHANGES} rows {first}-{last}." if last > first else f"Logged in {CHANGES} row {first}."
