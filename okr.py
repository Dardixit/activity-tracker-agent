"""Recount the monthly numbers on the OKRs tab from the Activity Log and Projects.

KR1 Networking, KR2 Outreach, KR5 Learning: Activity Log rows with Status
"Done", by the month of their Date.
KR3 LinkedIn posts: the same, for Personal Brand rows whose Activity or
Description contains both "LinkedIn" and "post" (case-insensitive).
KR4 Projects started: Projects rows by the month of their Started date
(rows without a date are not counted).
The category names are the CATEGORIES defaults; rename them here too if you
change CATEGORIES in .env.

Month columns are matched by month and year. Headers without a year (the
original "Aug" to "Nov") are taken as PLAIN_MONTH_YEAR; columns added by the
recount always carry the year, e.g. "Jan 2027", and go after the last column.
Months that have not started yet are left blank.

Run on its own with:  .venv/bin/python okr.py      (writes to the target Sheet)
"""

import re
from collections import Counter

import agent

OKR_TAB = "OKRs"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# The year of month headers that don't name one ("Aug", "Sep", ...).
PLAIN_MONTH_YEAR = 2026

# The first d.m.yyyy or d/m/yyyy in the text. Handles "26.10.2026, 17.00",
# "02/08/2026" (older rows, shown in UK format) and "21-22.10.2026".
DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})")
HEADER_RE = re.compile(r"^(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?$")


def month_of(text):
    """(year, month) of the first date in the text, or None."""
    m = DATE_RE.search(text or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return int(m.group(3)), int(m.group(2))


def header_month(label):
    """(year, month) for a month column header like "Aug" or "Jan 2027", else None."""
    m = HEADER_RE.match(label.strip())
    if not m:
        return None
    return int(m.group(2) or PLAIN_MONTH_YEAR), MONTHS.index(m.group(1)) + 1


def month_label(year, month):
    return f"{MONTHS[month - 1]} {year}"


def is_linkedin_post(r):
    text = (r.get("Activity", "") + " " + r.get("Description", "")).lower()
    return "linkedin" in text and "post" in text


# KR code -> which Activity Log rows count (KR4 counts Projects instead).
ACTIVITY_RULES = {
    "KR1": lambda r: r["Category"] == "Networking",
    "KR2": lambda r: r["Category"] == "Outreach",
    "KR3": lambda r: r["Category"] == "Personal Brand" and is_linkedin_post(r),
    "KR5": lambda r: r["Category"] == "Learning",
}


def count(activity_rows, project_rows):
    """{KR code: Counter{(year, month): n}} from tab rows that include the header."""
    counts = {kr: Counter() for kr in [*ACTIVITY_RULES, "KR4"]}
    header = activity_rows[0]
    for values in activity_rows[1:]:
        r = agent.row_as_dict(header, values)
        if r["Status"].strip() != "Done":
            continue
        month = month_of(r["Date"])
        for kr, rule in ACTIVITY_RULES.items():
            if month and rule(r):
                counts[kr][month] += 1
    header = project_rows[0]
    for values in project_rows[1:]:
        month = month_of(agent.row_as_dict(header, values).get(agent.PROJECT_STARTED, ""))
        if month:
            counts["KR4"][month] += 1
    return counts


def recount_edits(okr_rows, counts, today=None):
    """Edits that bring the OKRs tab in line with `counts` (empty if it already is)."""
    today = today or agent.now().date()
    current = (today.year, today.month)
    header = list(okr_rows[0])
    edits = []

    # Add a column for every started month that has a count but no column yet.
    existing = {header_month(h) for h in header} - {None}
    needed = {m for c in counts.values() for m, n in c.items() if n and m <= current}
    for year, month in sorted(needed - existing):
        col, label = len(header), month_label(year, month)
        header.append(label)
        edits.append({
            "tab": OKR_TAB, "row": 1, "col": col, "column": label, "old": "", "new": label,
            "reason": "OKR recount: new month column", "action": "add", "entry": "(month header)",
        })

    month_cols = [(i, h, header_month(h)) for i, h in enumerate(header) if header_month(h)]
    for row_number, values in enumerate(okr_rows[1:], start=2):
        kr = values[0].strip() if values else ""
        if kr not in counts:
            continue
        metric = values[1] if len(values) > 1 else kr
        for col, label, month in month_cols:
            new = counts[kr][month] if month <= current else ""  # not started yet: blank
            old = values[col] if col < len(values) else ""
            if old.strip() != str(new):
                edits.append({
                    "tab": OKR_TAB, "row": row_number, "col": col, "column": label, "old": old, "new": new,
                    "reason": f"OKR recount: {kr} {metric}, {label}", "action": "update", "kr": kr,
                    "entry": f"{kr} {metric}",
                })
    return edits


def recount(writer):
    """Recount and write the changed OKR cells. Returns (edits, logged rows)."""
    fresh = writer.read([agent.ACTIVITY, agent.PROJECTS, OKR_TAB])
    counts = count(fresh[agent.ACTIVITY], fresh[agent.PROJECTS])
    edits = recount_edits(fresh[OKR_TAB], counts)
    return edits, writer.apply(edits)


def describe(edits):
    """Short text for the Telegram reply, e.g. 'KR1 Oct: 6 -> 9'."""
    lines = []
    for e in edits:
        if e["row"] == 1:
            lines.append(f"  New column {e['new']}")
        else:
            new = e["new"] if e["new"] != "" else "(blank, month not started)"
            lines.append(f"  {e['kr']} {e['column']}: {e['old'] or '(empty)'} -> {new}")
    return "OKRs recounted:\n" + "\n".join(lines)


if __name__ == "__main__":
    import sheet_writer
    edits, logged = recount(sheet_writer.Writer())
    print(describe(edits) if edits else "OKRs already up to date.")
