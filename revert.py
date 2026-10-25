"""Undo changes listed in the Changes tab, by their Changes row numbers.

Run with:  .venv/bin/python revert.py 67-82 84          (ranges allowed)
           .venv/bin/python revert.py --test 67-82     (reason gets "test: " in front)

- A cell change is undone by writing the old value back.
- A new Activity Log row (logged with Source '' -> 'agent' or 'calendar') is deleted.
- A row moved to keep date order is not moved back as such: restoring its old
  Date puts it back in date order, so the "(whole row)" move rows are skipped.
Activity Log rows are found by their Entry ("date | activity"), because row
numbers shift when rows are inserted. Nothing is undone if any of the cells has
changed again since, or the row is locked. Each undo is logged in Changes.
Afterwards the OKRs are recounted, since deleted rows can change the counts.
"""

import sys

import agent
import okr
import sheet_writer
from sheet_writer import CHANGES, CHANGES_HEADER, WriteRefused, cell, log_row, update_cells

# Source values of rows the bot added itself (notes and calendar), which a revert deletes.
ADDED_BY = ("agent", "calendar")


def parse_numbers(args):
    numbers = []
    for a in args:
        first, _, last = a.partition("-")
        numbers += range(int(first), int(last or first) + 1)
    return numbers


def typed(tab, column, text):
    """A logged value (always text) back as the type it is in the Sheet."""
    if column == agent.LOCKED:
        return text == "TRUE"
    if (tab == okr.OKR_TAB or column == "#") and text.isdigit():
        return int(text)
    return text


def find_row(rows, tab, entry, logged_row):
    """The current Sheet row a Changes entry is about."""
    if tab != agent.ACTIVITY:
        return int(logged_row)  # Projects and OKRs rows don't move
    header = rows[0]
    matches = [n for n, values in enumerate(rows[1:], start=2)
               if sheet_writer.entry_for(tab, agent.row_as_dict(header, values)) == entry]
    if len(matches) != 1:
        raise WriteRefused(f"Found {len(matches)} {tab} rows for {entry!r}; expected exactly one.")
    return matches[0]


def main(args):
    test = "--test" in args
    numbers = parse_numbers([a for a in args if a != "--test"])
    if not numbers:
        sys.exit(__doc__)

    writer = sheet_writer.Writer()
    writer.reason_prefix = "test: " if test else ""
    changes = writer.read([CHANGES])[CHANGES]

    # Group the logged cells by the row they changed.
    groups = {}
    for n in numbers:
        values = changes[n - 1] + [""] * len(CHANGES_HEADER)
        log = dict(zip(CHANGES_HEADER, values))
        if log["Column"] == "(whole row)" and log["New value"].startswith("row "):
            print(f"Changes row {n} is a move to keep date order: undone by restoring the Date.")
            continue
        groups.setdefault((log["Tab"], log["Entry"], log["Row"]), []).append((n, log))
    # Read every tab the logged rows touched (e.g. Calendar titles too).
    fresh = {CHANGES: changes, **writer.read({tab for tab, _, _ in groups})}

    requests, logs, deletes = [], [], []
    # The Activity Log as it will read after the restores, to put rows whose
    # Date is restored back in date order.
    simulated = [list(r) for r in fresh.get(agent.ACTIVITY, [])]
    redated = []
    for (tab, entry, logged_row), items in groups.items():
        rows = fresh[tab]
        header = rows[0]
        row = find_row(rows, tab, entry, logged_row)
        current = agent.row_as_dict(header, rows[row - 1])
        for n, log in items:
            if current.get(log["Column"], "") != log["New value"]:
                raise WriteRefused(
                    f"{tab} row {row}, {log['Column']} changed again since Changes row {n} "
                    f"(now {current.get(log['Column'], '')!r}, logged {log['New value']!r})."
                )
        if tab == agent.ACTIVITY and current.get(agent.LOCKED, "").upper() == "TRUE":
            raise WriteRefused(f"{tab} row {row} is locked.")

        by_column = {log["Column"]: log for _, log in items}
        source = by_column.get(agent.SOURCE)
        changes_rows = f"{items[0][0]}-{items[-1][0]}" if len(items) > 1 else str(items[0][0])
        if tab == agent.ACTIVITY and source and source["Old value"] == "" and source["New value"] in ADDED_BY:
            deletes.append(row)
            logs.append(log_row(tab, row, entry, "(whole row)", f"row {row}", "deleted",
                                f"Revert of Changes rows {changes_rows} (row added by the agent)"))
            print(f"Delete {tab} row {row}: {entry}")
            continue
        for n, log in items:
            col = header.index(log["Column"])
            requests.append(update_cells(writer.sheet_ids[tab], row - 1, col,
                                         [[cell(typed(tab, log["Column"], log["Old value"]))]]))
            if tab == agent.ACTIVITY:
                r = simulated[row - 1]
                r += [""] * (col + 1 - len(r))
                r[col] = log["Old value"]
                if log["Column"] == "Date":
                    redated.append((r, f"Revert of Changes row {n}"))
            logs.append(log_row(tab, row, entry, log["Column"], log["New value"], log["Old value"],
                                f"Revert of Changes row {n}"))
            print(f"Restore {tab} row {row}, {log['Column']}: {log['New value']!r} -> {log['Old value']!r}")

    # Deletes after the cell edits, from the bottom up so row numbers stay valid.
    for row in sorted(deletes, reverse=True):
        requests.append({"deleteDimension": {"range": {
            "sheetId": writer.sheet_ids[agent.ACTIVITY], "dimension": "ROWS", "startIndex": row - 1, "endIndex": row,
        }}})
        simulated.pop(row - 1)
    # Rows whose old Date is back move to their place in date order.
    if redated:
        moves, move_logs, _ = sheet_writer.reposition(simulated, writer.sheet_ids[agent.ACTIVITY], redated)
        requests += moves
        logs += move_logs

    first, last = writer.commit(requests, logs, changes_rows_before=len(fresh[CHANGES]))
    print(f"Logged in {CHANGES} rows {first}-{last}.")

    edits, logged = okr.recount(writer)
    if edits:
        print(okr.describe(edits) + "\n" + sheet_writer.logged_text(logged))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except WriteRefused as err:
        sys.exit(f"Nothing was reverted: {err}")
