# activity-tracker-agent

A Telegram bot that keeps an activity tracker (a Google Sheet) up to date.
You send it a short note ("met Alex at the library today, follow up next week"); a
Claude agent finds the matching rows, decides whether to update one or add new
ones, and the bot writes the change and replies with what it changed. Google
Calendar is a second source: events are matched to existing rows or added, and
the bot asks on Telegram about unclear events and, afterwards, whether you
attended. Every write is logged in the Sheet's `Changes` tab.

Python 3.12 (`.python-version`), virtualenv in `.venv`. The venv has no pip:
install packages with `uv pip install --python .venv/bin/python <package>`.

## Files

| File | What it does |
|---|---|
| `bot.py` | The live bot: polls Telegram, runs the agent on notes, writes, replies, sends questions (one per message, with buttons), runs the calendar check at start and hourly. Restarts itself when any `.py` file changes. |
| `agent.py` | The agent: prompt, tools (`search_rows`, `propose_change`, `ask_user`), the rule checks (`check_change`), pausing and continuing runs for questions, Helsinki clock (`now()`). Never writes itself. |
| `sheet_writer.py` | All writes. One atomic `batchUpdate` per write: cell changes + their `Changes` rows. Re-reads every cell first; inserts Activity Log rows in date order; moves rows whose Date changes. |
| `calendar_source.py` | Reads the shared Google Calendar (read-only), today to 2 months ahead, one item per occurrence. |
| `calendar_sync.py` | Calendar logic: link events to rows, add missing ones, title decisions, moves, cancellations, attendance questions, answers. `--practice` / `--apply-plan`. |
| `okr.py` | Recounts the OKRs tab month columns after each run. |
| `retry.py` | Retries network errors (timeouts, connection/DNS errors, HTTP 429/5xx) from Telegram, Google and Claude, waiting 2, 4, 8 ... up to 60 s. |
| `sheets.py` | Settings (Sheet selection, tab names, categories) and Google credentials. |
| `setup_sheet.py` | One-time Sheet setup (Locked, Source, Event ID columns; Changes tab; hidden Calendar titles tab). Safe to run again. |
| `test_bot.py` | Runs `bot.main()` with Telegram faked; test writes are labelled. |
| `revert.py` | Undoes Changes rows. |
| `repair_rows.py` | Puts agent rows that are out of date order or lack dropdowns back in place. |
| `dry_run.py` | Runs the agent on a note and prints the proposal; never writes. |
| `timing.py` | Times Telegram, Sheet reads and model calls. |

Local files that git ignores: `.env`, `service-account.json` (Google key),
`last_update_id.txt` (Telegram offset), `pending_questions.json` (open
questions by Telegram message id), `calendar_state.json` (snoozed/ignored
calendar questions), `calendar_plan.json` (a practice plan awaiting approval).

## The Sheet

Tabs the bot uses: `Activity Log` (Date, Status, Category, Activity,
Description, Outcome, Locked, Source, Event ID), `Projects` (#, Project,
Link, Started, Deadline, ...; the code relies on `#`, `Project`, `Link` and
`Started`), `OKRs`,
`Changes` (Timestamp, Tab, Row, Entry, Column, Old value, New value, Reason,
Error), and the hidden `Calendar titles` (Title, Track, Decided by, Date).
The Activity Log and Projects tab names are settings (`ACTIVITY_TAB`,
`PROJECTS_TAB`), as is the category list (`CATEGORIES`); see `.env.example`.
`Locked`, `Source` and `Event ID` are set by code only; the agent never sees
them as editable. The Error checkbox in `Changes` is for the user to tick on
wrong agent changes.

## Rules (set by the user; keep them)

Most are enforced in code, not only in the prompt. Where both exist, keep both.

- **Locked rows are read only.** An Activity Log row is locked when its
  `Locked` cell is TRUE (rows 2-171 at setup). Locking goes by the column,
  never by row number, because rows shift when rows are inserted. No Locked
  column means every row is treated as locked. Checked in
  `agent.check_change` and again in `Writer.apply`.
- **Projects rows 2-23: any cell may change except Link.** Checked in both places.
- **Names only from the message.** Every person, company or event name the
  agent writes, in a new row or an update, must appear in what the user wrote
  (the note or answers). Names already in the cell being updated may stay.
  For calendar rows the event's title, location and description count as the
  source. `agent.unknown_names`.
- **Same first name: ask before adding.** Before a note adds a row that names
  a person, every row (open or done, locked or not, both tabs) is searched for
  that first name. If any row has it, the add is rejected until the agent
  called `ask_user` with `person`, which Telegram shows with the buttons
  "Same person, update existing row" / "Same person, new row" / "Different
  person". People are the add's `people` list plus any capitalised word after
  "with"/"met". `agent.person_problem`, checked in `run_tool` (notes only, not
  calendar rows).
- **Done/Missed only for past dates.** Status Done or Missed is allowed only
  when the row's date (last day of a range) is today or earlier, Helsinki
  time; otherwise the agent must ask. `agent.past_status_problem`, checked in
  the agent and in the writer.
- **Never delete rows.** Cancelled calendar events keep their row: Outcome
  becomes "Cancelled", Status is left as it is. The only code that deletes
  rows is `revert.py` / `repair_rows.py`, for undoing or re-placing rows the
  agent itself added.
- **Contradictions are asked, not written.** If a note contradicts a row
  (date, status), the agent calls `ask_user`; nothing from that run is written.
- **Change only what the message gives new information for**; never reword or
  reformat existing cell text.
- **New Activity Log rows go in date order** (before the first later or
  undated row), as real inserts that take formatting and dropdowns from the
  row above. A follow-up in a note ("follow up next week") is its own Outreach
  row, Status Pending, dated the Monday of next week unless a day is named.
- **Category only from `CATEGORIES`, Status only from values already used in
  the Sheet.** `agent.allowed_values`.
- **Europe/Helsinki for all dates**: the agent's "today", OKR months, Changes
  timestamps. Use `agent.now()`, never `date.today()` or `datetime.now()`.
- **Every write is logged in `Changes` in the same atomic request.** Calendar
  writes have reasons starting with `calendar:`.
- **Telegram replies are plain text**, no markdown (`bot.plain` strips it).
- **Only chat `TELEGRAM_CHAT_ID` is answered**; everyone else is ignored,
  button taps included.
- **Network errors never stop the bot.** Telegram, Sheets, Calendar and
  Claude calls go through `retry.call` / `retry.execute`: one log line per
  failure, then a retry with growing delay (max 60 s). The polling loop
  retries forever; inside a run a call gives up after 8 tries and the run's
  handler reports it. Sheet writes are not blindly resent: after a network
  error `Writer.send_once` checks the Changes row count to see whether the
  atomic write landed (`WriteUnknown` if it cannot tell).
- **Calendar: nothing is written before approval** on a first run: use
  `--practice`, show the report, then `--apply-plan`.

## Working on the code

- **Work on the test Sheet only** (`SHEET_TARGET=test`, the default). The
  writer refuses to write to the real Sheet unless `SHEET_TARGET=real`. The
  service account can edit the real Sheet (`setup_sheet.py` has run there), so
  that check is the only thing between a test and real data.
- **The live bot restarts itself when a `.py` file in this folder changes**,
  and with `CALENDAR_SYNC=on` it runs a calendar check on every restart. So
  build larger changes in a git worktree (`git worktree add ../tracker-<name> -b <name>`,
  copy `.env` and `service-account.json` into it), test there, then merge into
  `main` and remove the worktree.
- **Test labelling:** live tests go through `test_bot.py`, which prefixes
  every `Changes` reason with `test: `. Start test messages with `test:`, list
  every test message sent in the report, say plainly when test data is
  invented, never use invented people as if real, and revert test writes
  afterwards with `revert.py --test`. Prefer offline checks (rule functions,
  stubbed writer, scripted fake model) over writes.
- Commit only when asked. Commit messages end with the Co-Authored-By line.

## How to run

```
.venv/bin/python bot.py                          # the bot (Ctrl+C to stop)
.venv/bin/python dry_run.py "met Alex today"     # agent proposal only, no writes
.venv/bin/python setup_sheet.py                  # one-time Sheet setup
.venv/bin/python okr.py                          # recount the OKRs tab
.venv/bin/python calendar_sync.py --practice     # calendar decisions, nothing written, saved to calendar_plan.json
.venv/bin/python calendar_sync.py --apply-plan   # write the approved plan (within 24 h), send its questions
.venv/bin/python timing.py [model ...]           # timing per step
```

## How to test

```
.venv/bin/python test_bot.py "test: had coffee with ... today"
.venv/bin/python test_bot.py "test: note" "reply: an answer"      # reply to the latest question
.venv/bin/python test_bot.py --test-questions "tap:1:no" "tap:2:skip" "3. yes"   # fake calendar questions
.venv/bin/python test_bot.py --test-questions "1 yes, 2 no" "tap:confirm:answers"
```

`reply:3: text` replies to calendar question 3; `tap:3:yes|no|skip` taps its
button; `tap:confirm:answers|note` answers the "is this a list of answers?"
question; `tap:note:same_update|same_new|different` taps a same-name button; `--calendar` turns the calendar check on. Test questions and the
Telegram offset use their own files, never the live bot's.

## How to revert

```
.venv/bin/python revert.py 67-82 84          # undo these Changes rows
.venv/bin/python revert.py --test 392-401    # same, reason marked as test cleanup
.venv/bin/python repair_rows.py              # re-place agent rows out of date order / without dropdowns
```

`revert.py` restores old cell values, deletes rows the agent added, finds
Activity Log rows by their Entry (row numbers shift), refuses if a cell
changed since, and puts rows whose old Date it restores back in date order.
Revert newer Changes rows first when the same cell changed twice.

## Settings (`.env`, see `.env.example`)

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`
(default `claude-haiku-5-5`), `GOOGLE_SHEET_ID` (real Sheet),
`GOOGLE_TEST_SHEET_ID`, `SHEET_TARGET` (`test` or `real`),
`GOOGLE_CALENDAR_ID`, `CALENDAR_SYNC` (`on` for hourly calendar checks),
`ACTIVITY_TAB` (default `Activity Log`), `PROJECTS_TAB` (default `Projects`),
`CATEGORIES` (default `Networking,Outreach,Project,Meeting,Learning,Personal Brand,Preparation`;
the OKR rules in `okr.py` use these names).
The bot reads `.env` at start; after changing it, restart the bot.

## Open items

Kept in `TODO.md`:
- Before deploying to GitHub Actions: switch Claude API auth from API key to identity federation.
- Switching to the real Sheet: share it with the service account as Editor,
  set `SHEET_TARGET=real`, run `setup_sheet.py` there once.
- Before GitHub Actions: the local state files (`pending_questions.json`,
  `calendar_state.json`, `last_update_id.txt`) need a durable home.
- New Projects rows are appended without copying formatting from the row above.
- OKR month headers without a year ("Aug" to "Nov") are taken as 2026 (`okr.PLAIN_MONTH_YEAR`).
