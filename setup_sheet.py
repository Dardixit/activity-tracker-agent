"""One-time setup of the target Sheet for agent writes. Safe to run again:
anything that already exists is skipped.

- Activity Log: adds a Locked column (checkbox, TRUE for rows 2-171) and a
  Source column ("manual" for every existing row)
- Projects: adds a Source column ("manual" for every existing row)
- Adds the Changes tab, the log of every write, or brings an existing one up
  to the current layout (the Entry column)
- Activity Log: adds an Event ID column (empty), the link to Google Calendar events
- Adds the hidden Calendar titles tab: per-title decisions for calendar events
- Adds the hidden Agent state tab: the bot's state between GitHub Actions runs
  (state.py); its later writes are not logged in Changes

Each step is logged as one summary row per column in Changes, and all of it
is sent as one all-or-nothing request.

Run with:  .venv/bin/python setup_sheet.py
"""

import agent
import calendar_sync
import sheet_writer
import state
from sheet_writer import CHANGES, CHANGES_HEADER, cell, log_row, update_cells

REASON = "Setup for agent writes"


def main():
    writer = sheet_writer.Writer()
    fresh = writer.read([agent.ACTIVITY, agent.PROJECTS])
    requests, logs = [], []

    changes_id = writer.sheet_ids.get(CHANGES)
    changes_rows_before = None
    if changes_id is None:
        changes_id = max(writer.sheet_ids.values()) + 1
        changes_rows_before = 1  # just the header written below
        requests.append({"addSheet": {"properties": {
            "sheetId": changes_id, "title": CHANGES,
            "gridProperties": {"frozenRowCount": 1, "columnCount": len(CHANGES_HEADER)},
        }}})
        header = [{**cell(h), "userEnteredFormat": {"textFormat": {"bold": True}}} for h in CHANGES_HEADER]
        requests.append({"updateCells": {
            "start": {"sheetId": changes_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": header}],
            "fields": "userEnteredValue,userEnteredFormat.textFormat.bold",
        }})
        logs.append(log_row(CHANGES, "", "", "(tab)", "", "created", REASON))
    else:
        # Changes tabs made before the Entry column existed: insert it after Row.
        header = writer.read([CHANGES])[CHANGES][0]
        if "Entry" not in header:
            col = CHANGES_HEADER.index("Entry")
            requests.append({"insertDimension": {
                "range": {"sheetId": changes_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                "inheritFromBefore": True,
            }})
            requests.append(update_cells(changes_id, 0, col, [[cell("Entry")]]))
            logs.append(log_row(CHANGES, 1, "", "Entry", "", "column added (empty for older rows)", REASON))
            print(f"{CHANGES}: adding Entry as column {col + 1}.")

    def add_column(tab, name, values, summary):
        rows = fresh[tab]
        header = rows[0]
        if name in header:
            print(f"{tab}: {name} column already exists, skipped.")
            return
        sheet_id, col = writer.sheet_ids[tab], len(header)
        requests.append(update_cells(
            sheet_id, 0, col, [[cell(name)]] + [[cell(v)] for v in values], "userEnteredValue,dataValidation"
        ))
        # Give the new header cell the same look as the one to its left.
        requests.append({"copyPaste": {
            "source": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                       "startColumnIndex": col - 1, "endColumnIndex": col},
            "destination": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                            "startColumnIndex": col, "endColumnIndex": col + 1},
            "pasteType": "PASTE_FORMAT",
        }})
        header.append(name)  # so the next column added to this tab goes one further right
        logs.append(log_row(tab, f"2-{len(rows)}", "", name, "", summary, REASON))
        print(f"{tab}: adding {name} as column {col + 1}.")

    last_activity = len(fresh[agent.ACTIVITY])
    add_column(
        agent.ACTIVITY, agent.LOCKED,
        [n <= agent.ACTIVITY_LOCKED_UNTIL for n in range(2, last_activity + 1)],
        f"TRUE for rows 2-{agent.ACTIVITY_LOCKED_UNTIL}, FALSE for rows "
        f"{agent.ACTIVITY_LOCKED_UNTIL + 1}-{last_activity}",
    )
    add_column(agent.ACTIVITY, agent.SOURCE, ["manual"] * (last_activity - 1), "manual")
    add_column(agent.PROJECTS, agent.SOURCE, ["manual"] * (len(fresh[agent.PROJECTS]) - 1), "manual")
    add_column(agent.ACTIVITY, agent.EVENT_ID, [""] * (last_activity - 1), "(empty)")

    if calendar_sync.TITLES_TAB not in writer.sheet_ids:
        titles_id = max(list(writer.sheet_ids.values()) + [changes_id]) + 1
        requests.append({"addSheet": {"properties": {
            "sheetId": titles_id, "title": calendar_sync.TITLES_TAB, "hidden": True,
            "gridProperties": {"frozenRowCount": 1, "columnCount": len(calendar_sync.TITLES_HEADER)},
        }}})
        header = [{**cell(h), "userEnteredFormat": {"textFormat": {"bold": True}}} for h in calendar_sync.TITLES_HEADER]
        requests.append({"updateCells": {
            "start": {"sheetId": titles_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": header}],
            "fields": "userEnteredValue,userEnteredFormat.textFormat.bold",
        }})
        # Dropdowns, so a hand edit can only say yes/no and you/model.
        for column, options in (("Track", ["yes", "no"]), ("Decided by", ["you", "model"])):
            col = calendar_sync.TITLES_HEADER.index(column)
            requests.append({"setDataValidation": {
                "range": {"sheetId": titles_id, "startRowIndex": 1, "startColumnIndex": col, "endColumnIndex": col + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": o} for o in options]},
                         "strict": True, "showCustomUi": True},
            }})
        logs.append(log_row(calendar_sync.TITLES_TAB, "", "", "(tab)", "", "created (hidden)", REASON))
        print(f"Adding the hidden {calendar_sync.TITLES_TAB} tab.")

    if state.TAB not in writer.sheet_ids:
        taken = list(writer.sheet_ids.values()) + [changes_id] + [
            r["addSheet"]["properties"]["sheetId"] for r in requests if "addSheet" in r]
        state_id = max(taken) + 1
        requests.append({"addSheet": {"properties": {
            "sheetId": state_id, "title": state.TAB, "hidden": True,
            "gridProperties": {"frozenRowCount": 1, "columnCount": state.MAX_CHUNKS + 2},
        }}})
        header = [{**cell(h), "userEnteredFormat": {"textFormat": {"bold": True}}} for h in state.HEADER]
        requests.append({"updateCells": {
            "start": {"sheetId": state_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": header}],
            "fields": "userEnteredValue,userEnteredFormat.textFormat.bold",
        }})
        logs.append(log_row(state.TAB, "", "", "(tab)", "", "created (hidden)", REASON))
        print(f"Adding the hidden {state.TAB} tab.")

    if not logs:
        print("Already set up, nothing to do.")
        return
    first, last = writer.commit(requests, logs, changes_sheet_id=changes_id, changes_rows_before=changes_rows_before)
    print(f"Done. Logged in {CHANGES} rows {first}-{last}.")


if __name__ == "__main__":
    main()
