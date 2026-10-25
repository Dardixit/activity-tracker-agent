"""Repair agent-added Activity Log rows: put them in date order, with the
formatting and dropdowns of the rows around them.

Rows the agent wrote before inserts-by-date existed were appended at the
bottom as plain text. Every Source=agent row that is out of date order, or has
no Status dropdown, is deleted and inserted again at its date position with its
values unchanged. Each moved row is logged as one row in Changes.

Safe to run again: rows already in order with dropdowns are left alone.

Run with:  .venv/bin/python repair_rows.py
"""

import agent
import sheets
import sheet_writer
from sheet_writer import date_order, entry_for, insert_position, insert_row_requests, log_row

REASON = "Repair: moved to date order; formatting and dropdowns copied from the row above"


def rows_with_dropdown(writer, header, last_row):
    """Sheet row numbers whose Status cell has a dropdown."""
    col = chr(ord("A") + header.index("Status"))
    meta = writer.service.spreadsheets().get(
        spreadsheetId=sheets.SHEET_ID,
        ranges=[f"'{agent.ACTIVITY}'!{col}2:{col}{last_row}"],
        fields="sheets(data(rowData(values(dataValidation))))",
    ).execute()
    row_data = meta["sheets"][0]["data"][0].get("rowData", [])
    return {n for n, r in enumerate(row_data, start=2) if r.get("values", [{}])[0].get("dataValidation")}


def out_of_order(rows):
    """Sheet row numbers that are earlier than a row above them, or below an undated row."""
    bad, latest, undated_above = set(), None, False
    for n, values in enumerate(rows[1:], start=2):
        key = agent.date_key(values[0] if values else "")
        if key is None:
            undated_above = True
            continue
        if undated_above or (latest and key < latest):
            bad.add(n)
        latest = max(latest, key) if latest else key
    return bad


def main():
    writer = sheet_writer.Writer()
    rows = writer.read([agent.ACTIVITY])[agent.ACTIVITY]
    header = rows[0]
    source_col = header.index(agent.SOURCE)
    dropdown = rows_with_dropdown(writer, header, len(rows))
    misplaced = out_of_order(rows)

    targets = [
        n for n, values in enumerate(rows[1:], start=2)
        if len(values) > source_col and values[source_col] == "agent" and (n in misplaced or n not in dropdown)
    ]
    if not targets:
        print("All agent rows are in date order with dropdowns. Nothing to do.")
        return

    sheet_id = writer.sheet_ids[agent.ACTIVITY]
    # Delete from the bottom up, so the row numbers above stay valid.
    requests = [
        {"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n}}}
        for n in sorted(targets, reverse=True)
    ]
    remaining = [values for n, values in enumerate(rows, start=1) if n not in targets]

    moved = []
    for n in targets:
        values = agent.row_as_dict(header, rows[n - 1])
        values[agent.LOCKED] = values[agent.LOCKED].strip().upper() == "TRUE"
        moved.append({"old_row": n, "values": values})

    logs = []
    for m in sorted(moved, key=date_order):
        row = insert_position(remaining, agent.date_key(m["values"]["Date"]))
        requests += insert_row_requests(sheet_id, row, [sheet_writer.cell(m["values"][h]) for h in header])
        remaining.insert(row - 1, [sheet_writer.shown(m["values"][h]) for h in header])
        entry = entry_for(agent.ACTIVITY, m["values"])
        logs.append(log_row(agent.ACTIVITY, row, entry, "(whole row)", f"row {m['old_row']}", f"row {row}", REASON))
        print(f"Row {m['old_row']} -> row {row}: {entry}")

    first, last = writer.commit(requests, logs)
    print(f"Done. Logged in {sheet_writer.CHANGES} rows {first}-{last}.")


if __name__ == "__main__":
    main()
