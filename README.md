# activity-tracker-agent

A Telegram bot that keeps an activity tracker in Google Sheets up to date.
You send it a short note, such as "met Alex at the library today, follow up
next week". A Claude agent finds the rows the note is about and decides
whether to update one or add new ones. The bot writes the change and replies
with what it changed. Google Calendar is a second source: events are linked
to existing rows or added, and the bot asks on Telegram about anything unclear.

Every write is logged in the Sheet, rows you mark as locked are never touched,
and when the agent is unsure, it asks you with buttons.

This is a showcase copy of a personal project. Names in examples are placeholders.

## Architecture

```
 Telegram note ─┐                              ┌─> Google Sheet
                ├─> bot.py ─> agent.py ────────┤     Activity Log, Projects, OKRs
 Google         │      │      Claude tool use  │     + Changes tab (log of every write)
 Calendar ──────┘      │      search_rows      │
 (read-only)           │      propose_change ──┼─> rule checks (code) ─> sheet_writer.py
                       │      ask_user ────────┘                          one atomic batchUpdate
                       │                                                   per write
                       └─<── question with buttons (Telegram) <── answer continues the run
```

- **Inputs.** `bot.py` long-polls Telegram for your notes and button taps. With
  `CALENDAR_SYNC=on` it also reads the calendar (`calendar_source.py`, read-only)
  at start and then every hour. On GitHub Actions it runs once an hour instead
  (`bot.py --once`, see "Run it on GitHub Actions").
- **Claude tool use.** `agent.py` gives the model three tools:
  - `search_rows`: finds candidate rows in both tabs.
  - `propose_change`: an `add` or `update` of named columns.
  - `ask_user`: pauses the run with a question.

  Every proposal is checked in code before it is accepted. A rejected proposal
  goes back to the model with the reason, so it can fix it. The agent itself
  never writes.
- **Sheets out.** `sheet_writer.py` re-reads every cell it is about to change and
  refuses if anything changed since the agent read it. It then sends the cell
  changes and their log rows as one atomic `batchUpdate`. New Activity Log rows
  are inserted in date order and take formatting and dropdowns from the row above.
- **Change log.** Every write appends rows to the `Changes` tab in that same
  request: timestamp, tab, row, entry, column, old value, new value and reason,
  plus an Error checkbox for you to tick on bad changes. `revert.py` undoes
  Changes rows by number.
- **Locked rows.** A `Locked` checkbox column marks rows the agent may only read.
  The check reads the column, so it still holds when inserts shift rows. It
  runs in the agent's checks and again in the writer.
- **Ask-the-user buttons.** Questions arrive as Telegram messages. A paused run
  is saved to disk and continued with your answer as the tool result. Same-name
  checks and calendar questions come with inline buttons; you can also reply
  in words, or `/skip`.
- **Calendar sync.** `calendar_sync.py` links events to rows, adds missing ones,
  moves rows whose event moved, and asks about cancellations and attendance.
  It remembers per-title decisions in a hidden `Calendar titles` tab.
- **OKRs.** `okr.py` recounts the month columns of the OKRs tab after each run.

## Guardrails

Each rule below is marked (code) when the code enforces it, or (prompt) when only the model's instructions do.

- **Locked rows are read only** (code), checked in the agent's checks and again
  in the writer.
- **No invented names** (code). Every person, company or event name the agent
  writes must appear in what you wrote (the note or your answers), or in the
  calendar event for calendar rows.
- **Same first name: ask before adding** (code). If a note's new row names a
  person whose first name is already in any row, the add is rejected until you
  have chosen "Same person, update existing row", "Same person, new row" or
  "Different person". Calendar rows use their own matching instead.
- **Calendar: same start time, ask before adding** (code). A new calendar
  event with the same date and start time as a row already linked to another
  event is not added; you choose "Same event, link to existing row",
  "Different event, add row" or "Skip".
- **Contradictions go to you as a question** (prompt). The model is told to ask
  when a note conflicts with a row's date or status. Once it asks, the code
  drops every change from that run, so nothing is written until you answer.
- **Done/Missed only for past dates** (code), Europe/Helsinki time, checked in
  the agent's checks and again in the writer.
- **Only allowed values** (code). Category must be one of `CATEGORIES`; Status
  one of the values already used in the Sheet.
- **Change only what the note gives new information for** (prompt). The
  model is told not to reword existing cell text.
- **Never delete rows** (code). The agent can only add or update, and the writer
  has no delete. A cancelled event's row keeps its place; the bot asks whether
  to set its Outcome to "Cancelled". Only `revert.py` and `repair_rows.py` delete
  rows, and only rows the agent added.
- **Every write is logged** (code) in the same atomic request as the change.
  The one exception is the hidden `Agent state` tab, which holds the bot's
  own bookkeeping, not tracker data.
- **One chat only** (code). Messages and button taps from any chat other than
  `TELEGRAM_CHAT_ID` are ignored.
- **Test Sheet by default** (code). The writer refuses to write to the real
  Sheet unless `SHEET_TARGET=real`.
- **Calendar: practice first** (code for the modes; the approval is up to you).
  `--practice` shows every planned link, new row and question without writing,
  and `--apply-plan` writes exactly that plan. With `CALENDAR_SYNC=on`, the
  hourly check writes without a practice run, so turn it on only after
  approving one.
- **Network errors never stop the bot** (code). Calls are retried with growing
  delays (up to 60 s). A timed-out write is resent only after the Changes tab
  shows that it did not land.

## Setup

1. **Python 3.12 environment**
   ```
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
2. **Telegram.** Create a bot with @BotFather and copy its token. Send the bot
   a message, then find your chat ID (for example from
   `https://api.telegram.org/bot<token>/getUpdates`).
3. **Google Cloud.** In a project, enable the Google Sheets API and the Google
   Calendar API. Create a service account and download its JSON key as
   `service-account.json` in this folder. Git ignores it.
4. **The Sheet.** It needs three tabs:
   - `Activity Log`, one row per activity: Date, Status, Category, Activity,
     Description, Outcome.
   - `Projects`, one row per project: `#`, `Project`, `Link`, `Started`, plus
     any columns you like (Deadline, Next steps, Outcome, ...).
   - `OKRs`, with KR codes in column A and month columns (Jan, Feb, ...).

   Make a test copy too. Share both Sheets with the service account's email as
   Editor.
5. **Calendar (optional).** Share the calendar with the service account's
   email ("See all event details").
6. **Settings.** Copy `.env.example` to `.env` and fill in:
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
   - `ANTHROPIC_API_KEY`
   - `GOOGLE_SHEET_ID` and `GOOGLE_TEST_SHEET_ID`
   - optionally `GOOGLE_CALENDAR_ID` and `CALENDAR_SYNC=on`
   - optionally `ACTIVITY_TAB` and `PROJECTS_TAB`, if your tabs have other names
   - optionally `CATEGORIES`, the Activity Log categories the agent may use.
     The default is Networking, Outreach, Project, Meeting, Learning, Personal
     Brand, Preparation; the OKR rules in `okr.py` count by these names.

   Keep `SHEET_TARGET=test` until you trust it.
7. **Prepare the Sheet** (once; safe to run again). This adds the Locked,
   Source and Event ID columns and the Changes and Calendar titles tabs.
   ```
   .venv/bin/python setup_sheet.py
   ```
   Rows locked at setup are set by `ACTIVITY_LOCKED_UNTIL` in `agent.py`.
8. **Try it without writing:**
   ```
   .venv/bin/python dry_run.py "met Alex at the library today, follow up next week"
   .venv/bin/python calendar_sync.py --practice      # then --apply-plan to write that plan
   ```
9. **Run the bot:**
   ```
   .venv/bin/python bot.py
   ```

`test_bot.py` runs the real bot loop with Telegram faked and labels every
logged reason `test:`. Use `revert.py --test <Changes rows>` to undo test writes.
`CLAUDE.md` has the full file list, rules and commands.

## Run it on GitHub Actions

The workflow is included: `.github/workflows/agent.yml`. It is scheduled
hourly, at minute 7 from 07:07 to 00:07 Europe/Helsinki time (daylight saving
time included; no runs from 01:00 to 06:59). Each run is `bot.py --once`: it
handles the waiting Telegram messages and button taps, runs the calendar check
and the OKR recount, and exits. What has to survive between runs (the Telegram
offset, open questions with their paused agent runs, calendar state) is kept
in the Sheet's hidden `Agent state` tab. Runs never overlap, each is stopped
after 6 minutes, and a failed run sends you a Telegram message.

**In this repository the workflow has no secrets.** Each scheduled run first
checks for them, finds none, prints which ones are missing and exits without
doing anything; the run still shows as successful. After you fork the
repository and add your own secrets and variables, the hourly run works.

### Secrets and variables

| Name | Kind | Holds |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | secret, required | The bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | secret, required | Your own chat ID; every other chat is ignored. |
| `GOOGLE_TEST_SHEET_ID` | secret, required | The ID of the test copy of the Sheet (from its URL). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | secret, required | The whole contents of the service account's JSON key file. |
| `GOOGLE_SHEET_ID` | secret, required for the real Sheet | The ID of the real Sheet. |
| `GOOGLE_CALENDAR_ID` | secret, optional | The calendar to read; the calendar check runs only when it is set. |
| `ANTHROPIC_FEDERATION_RULE_ID` | variable, required | The federation rule (`fdrl_...`) created below. |
| `ANTHROPIC_ORGANIZATION_ID` | variable, required | Your Anthropic organization ID (a UUID). |
| `ANTHROPIC_SERVICE_ACCOUNT_ID` | variable, required | The service account (`svac_...`) the rule maps to. |
| `ANTHROPIC_WORKSPACE_ID` | variable, optional | The workspace (`wrkspc_...`); needed when the rule covers more than one. |
| `SCHEDULE_SHEET_TARGET` | variable, optional | `test` (default) or `real`: the Sheet scheduled runs use. |
| `REAL_SHEET_APPROVED` | variable, optional | `true` allows runs on the real Sheet; until then they are refused. |
| `SCHEDULE_ENABLED` | variable, optional | `false` pauses the scheduled runs; manual runs still work. |
| `ACTIVITY_TAB`, `PROJECTS_TAB`, `CATEGORIES` | variables, optional | Tab names and categories, if yours differ from the defaults. |

There is no `ANTHROPIC_API_KEY`: the workflow authenticates to Claude with
workload identity federation, and a key in the environment would take
precedence over it.

### Workload identity federation for Claude

1. In the Claude Console (you need the admin or owner role), open
   **Settings > Workload identity > Connect workload** and choose
   **GitHub Actions**. The wizard creates the issuer
   (`https://token.actions.githubusercontent.com`), a service account and a
   federation rule.
2. Set the rule's match conditions. GitHub sends the token's subject in the
   ID-embedded format, with each name followed by its numeric ID:
   ```
   repo:<owner>@<owner-id>/<repo>@<repo-id>:ref:refs/heads/main
   ```
   Get both IDs with `gh api repos/<owner>/<repo> --jq '.owner.id, .id'`.
   Add the audience `https://api.anthropic.com` and the claim
   `ref` = `refs/heads/main`, so only runs from your `main` branch get a
   token. A rule in the plain `repo:<owner>/<repo>:ref:refs/heads/main`
   format does not match: the run fails with `401 Authentication failed`,
   and the Console's Workload identity history shows why.
3. Copy the rule ID, the service account ID, your organization ID and the
   workspace ID into the variables above.

The code (`agent.make_client`) asks GitHub for a fresh identity token for
every exchange, since each token may be used once and expires after about
5 minutes.

### Add the secrets

1. Fork this repository. Run `setup_sheet.py` once on your Sheet (locally,
   with `.env` set up) so it has the `Agent state` tab.
2. In your fork, open **Settings > Secrets and variables > Actions**.
3. On the **Secrets** tab, choose **New repository secret** for each secret
   above. For `GOOGLE_SERVICE_ACCOUNT_JSON`, paste the whole key file.
4. On the **Variables** tab, choose **New repository variable** for each
   variable above.
5. Open the **Actions** tab. If it shows that workflows are disabled in this
   fork, enable them.
6. Start one run by hand: **Actions > Agent > Run workflow**, Sheet `test`.
   When it succeeds, the hourly schedule takes over.

With the GitHub CLI, steps 3 and 4 are `gh secret set NAME` (it asks for the
value; `gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < service-account.json` for
the key) and `gh variable set NAME --body VALUE`.

GitHub runs scheduled workflows on a best-effort basis: a run can start a few
minutes late. In a public repository, GitHub also pauses the schedule after
60 days without any activity in the repository.
