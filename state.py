"""Agent state that has to survive between runs.

Keys:
- telegram_offset: the next Telegram update to fetch
- question:<message id>: one open question, with its paused agent run (bot.py)
- calendar_state: calendar questions you snoozed or said to ignore
- runner: "github-actions" once migrate_state.py has moved the bot there
- last_run: the Helsinki hour of the last one-shot run that finished (bot.py --scheduled)

STATE_BACKEND in .env picks where the state lives:
- "files" (the default): local files next to the code (last_update_id.txt,
  pending_questions.json, calendar_state.json); bot.py and calendar_sync.py
  read and write those themselves.
- "sheet": the hidden "Agent state" tab of the target Sheet, for GitHub
  Actions, where nothing on disk survives a run. The tab is read once per
  process; every change is written through right away.

Agent state writes are bookkeeping, not tracker data, so they are not logged
in the Changes tab (an approved exception to "every write is logged").
"""

import copy
import json
import os

import agent
import retry
import sheets

TAB = "Agent state"
HEADER = ["Key", "Updated", "Value"]
# A Sheets cell holds at most 50,000 characters, so long values (paused agent
# runs) are split over the columns after Key and Updated: C to Z.
CHUNK = 45000
MAX_CHUNKS = 24
LAST_COLUMN = "Z"

BACKEND = os.getenv("STATE_BACKEND", "").strip().lower() or "files"


def in_sheet():
    return BACKEND == "sheet"


class SheetStore:
    """The Agent state tab, read once and then kept in memory."""

    def __init__(self, writer=None):
        import sheet_writer  # here, because sheet_writer imports agent, which bot.py imports first
        self.writer = writer or sheet_writer.Writer()
        if TAB not in self.writer.sheet_ids:
            raise SystemExit(f"The Sheet has no {TAB!r} tab. Run setup_sheet.py first.")
        rows = self.writer.read([TAB])[TAB]
        # values: what the tab holds. get() and items() hand out copies, so a
        # caller changing a value in place can't make put() think it is saved.
        self.values, self.row_of, self.free = {}, {}, []
        for n, cells in enumerate(rows[1:], start=2):
            key = cells[0].strip() if cells else ""
            if not key:
                self.free.append(n)
                continue
            self.row_of[key] = n
            self.values[key] = json.loads("".join(cells[2:]) or "null")
        self.next_row = len(rows) + 1

    def get(self, key, default=None):
        value = self.values.get(key)
        return default if value is None else copy.deepcopy(value)

    def items(self, prefix):
        return {k: copy.deepcopy(v) for k, v in self.values.items() if k.startswith(prefix)}

    def put(self, changes):
        """Write {key: value} in one request; a value of None removes the key.
        Keys whose value is unchanged are skipped."""
        stamp = agent.now().strftime("%d.%m.%Y %H:%M:%S")
        data = []
        for key, value in changes.items():
            if value == self.values.get(key):
                continue
            if value is None:  # blank the row; it is reused for the next new key
                n = self.row_of.pop(key)
                self.values.pop(key)
                self.free.append(n)
                cells = [""] * (MAX_CHUNKS + 2)
            else:
                text = json.dumps(value, ensure_ascii=False)
                chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
                if len(chunks) > MAX_CHUNKS:
                    raise ValueError(f"{key}: {len(text)} characters is too long for one {TAB} row.")
                if key not in self.row_of:
                    self.row_of[key] = self.free.pop(0) if self.free else self._new_row()
                n = self.row_of[key]
                self.values[key] = copy.deepcopy(value)
                cells = [key, stamp] + chunks + [""] * (MAX_CHUNKS - len(chunks))
            data.append({"range": f"'{TAB}'!A{n}:{LAST_COLUMN}{n}", "values": [cells]})
        if not data:
            return
        # Setting cells to fixed values is safe to repeat, so a network error is
        # simply retried (unlike the tracker writes in sheet_writer.py).
        retry.execute(self.writer.service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheets.SHEET_ID, body={"valueInputOption": "RAW", "data": data}), "Sheets state write")

    def _new_row(self):
        n = self.next_row
        self.next_row += 1
        return n


_store = None


def store():
    """The Agent state tab of the target Sheet (STATE_BACKEND=sheet)."""
    global _store
    if _store is None:
        _store = SheetStore()
    return _store


def runner_in_sheet():
    """Who the Agent state tab says runs the bot ("github-actions"), or None.
    Read-only; works whatever STATE_BACKEND is, and None if there is no tab."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    creds, _ = sheets.load_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    try:
        rows = agent.read_tabs(service, [TAB])[0]
    except HttpError:
        return None
    for cells in rows[1:]:
        if cells and cells[0].strip() == "runner":
            return json.loads("".join(cells[2:]) or "null")
    return None
