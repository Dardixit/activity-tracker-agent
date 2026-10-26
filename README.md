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
  at start and then every hour. On GitHub Actions it runs as `bot.py --once`
  every 30 minutes instead, started by an external cron service (see "Run it
  on GitHub Actions" and "Trigger it from an external cron service").
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
- **Same day, same person or activity: ask before adding** (code). Before the
  calendar adds a row, every row on that date (linked or not) is searched for
  the same person, company or activity name; a note that adds a row matching a
  calendar row on the same day is checked the same way. If one matches, you
  choose "Same, link to existing row", "Different, add row" or "Skip". Your
  own names (`OWNER_NAMES`) never count, since calendar titles often carry them.
- **Done and Missed rows are final** (code). A row dated today or earlier with
  Status Done or Missed never gets a new date from the calendar. If its event
  now shows a later date (an invite reused for the next meeting), a new row is
  added for that date and the past row stays as it is.
- **One refused change never stops a calendar check** (code). The writer leaves
  out what the rules refuse and writes the rest; the bot asks "Try again next
  check", "Leave this event out" or "Skip". Refused again after "Try again",
  the event is left out and the check says so. Note runs stay all-or-nothing.
- **A question answered once is never sent again** (code). Every answer leaves
  a trace the next check sees (the row's status or link, a title decision, or
  the calendar state), also when a rule refused the change the answer asked
  for. The one planned exception: "Skip" asks again after 7 days.
  `test_answers.py` checks every question kind and answer.
- **Open questions survive code changes** (code). A paused run is saved with
  Claude's thinking blocks, which only fit the prompt and tools they were made
  with; the bot sends `thinking.block_binding.prefix_mismatch_behavior =
  "drop_block"`, so after a code change such blocks are dropped instead of
  making the request fail. `test_continue.py` checks it.
- **Telegram belongs to one Sheet** (code). There is one bot and one chat but a
  test Sheet and a real Sheet, each with its own open questions. Only runs on
  the Sheet named by `TELEGRAM_SHEET_TARGET` fetch your taps and replies or
  send messages, so a test run can never take them away. A tap on a question
  that is no longer open is answered, never dropped silently.
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

The workflow is included: `.github/workflows/agent.yml`. It is started every
30 minutes by an external cron service (see "Trigger it from an external cron
service"), with GitHub's own schedule as an optional backup. Each run is
`bot.py --once`: it
handles the waiting Telegram messages and button taps, runs the calendar check
and the OKR recount, and exits. What has to survive between runs (the Telegram
offset, open questions with their paused agent runs, calendar state) is kept
in the Sheet's hidden `Agent state` tab. Runs never overlap, each is stopped
after 6 minutes, and a failed run sends you a Telegram message.

**In this repository the workflow has no secrets.** Each scheduled run first
checks for them, finds none, prints which ones are missing and exits without
doing anything; the run still shows as successful. After you fork the
repository, add your own secrets and variables and set up the trigger, the
runs work.

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
| `SCHEDULE_SHEET_TARGET` | variable, optional | `test` (default) or `real`: the Sheet scheduled runs and the default manual run use. Only runs on this Sheet use Telegram. |
| `REAL_SHEET_APPROVED` | variable, optional | `true` allows runs on the real Sheet; until then they are refused. |
| `SCHEDULE_ENABLED` | variable, optional | `false` pauses GitHub's backup schedule; runs started by the external trigger or by hand still work. |
| `ACTIVITY_TAB`, `PROJECTS_TAB`, `CATEGORIES` | variables, optional | Tab names and categories, if yours differ from the defaults. |
| `OWNER_NAMES` | variable, optional | Your own first and last name, comma-separated; the duplicate check ignores them. |

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
   When it succeeds, set up the trigger in the next section.

With the GitHub CLI, steps 3 and 4 are `gh secret set NAME` (it asks for the
value; `gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < service-account.json` for
the key) and `gh variable set NAME --body VALUE`.

## Trigger it from an external cron service

GitHub's own `schedule` trigger is best-effort: runs start late, and at busy
times GitHub skips them. In practice it skipped most of this bot's hourly
runs. The recommended setup is therefore an external cron service that calls
the GitHub API to start the workflow (a `workflow_dispatch` event) every
30 minutes. The example below uses [cron-job.org](https://cron-job.org), which
is free; any service that can send a POST request with headers works.

### A fine-grained token for the trigger

The cron service needs a GitHub token that can start this one workflow and
nothing else:

1. On GitHub, open **Settings > Developer settings > Personal access tokens >
   Fine-grained tokens > Generate new token**.
2. **Repository access:** "Only select repositories", and pick your fork only.
3. **Permissions:** under Repository permissions, **Actions: Read and write**.
   GitHub adds Metadata: Read-only by itself. Leave everything else at
   "No access".
4. **Expiration:** set one (up to a year) and note the date. When the token
   expires, the cron service gets `401` and the bot stops running until you
   put a new token in. GitHub emails you before it expires.
5. Copy the token; it is shown only once. Keep it only in the cron service.

With this token, a leak can start or cancel your workflow runs, but cannot
read your secrets, change your code or reach any other repository.

### The scheduled request

On cron-job.org, add a new scheduled request with:

- **URL:**
  ```
  https://api.github.com/repos/<owner>/<repo>/actions/workflows/agent.yml/dispatches
  ```
- **Schedule:** custom, minutes `7` and `37`, hours `0` and `7-23`, every day,
  time zone `Europe/Helsinki` (or yours). Minute 7 keeps away from the busy
  top of the hour; no runs from 01:00 to 06:59.
- **Request method:** `POST`
- **Headers:**
  ```
  Accept: application/vnd.github+json
  Authorization: Bearer <your token>
  X-GitHub-Api-Version: 2022-11-28
  Content-Type: application/json
  ```
- **Request body:**
  ```json
  {"ref": "main", "inputs": {"sheet": "real"}}
  ```
  Use `"test"` until you have approved the real Sheet (`REAL_SHEET_APPROVED`).
  `"scheduled"` uses `SCHEDULE_SHEET_TARGET`.
- **Notifications:** turn on the failure notification.

Use the service's test run: GitHub answers `204 No Content` and a run appears
under **Actions**. `401` or `403` means the token has expired or lacks the
Actions permission, `404` a wrong URL or a token without access to the
repository, `422` a wrong body (for example a branch or input that doesn't
exist).

### Actions minutes

GitHub bills each run as whole minutes, rounded up. A run takes 20-100
seconds, about 1.2 billed minutes on average. Every 30 minutes for 18 hours a
day is 36 runs, about 43 minutes a day or 1,350 a month. Public repositories
don't pay for standard GitHub-hosted runners. For a private copy, GitHub Free
includes 2,000 minutes a month and Pro 3,000; the backup schedule below adds
about 750.

### GitHub's schedule as an optional backup

The workflow keeps a `schedule` trigger: every hour at minute 37 UTC, run as
`bot.py --once --scheduled`. Such a run does nothing from 01:00 to 06:59
Helsinki time, or when a run already finished in the same Helsinki hour (the
Agent state tab remembers the last run), so it only fills in when the cron
service missed an hour. Set the variable `SCHEDULE_ENABLED=false` to turn it
off. Two runs in one hour do no harm either way: runs never overlap, each
Telegram update is handled once, and open questions are not sent again.
In a public repository, GitHub also pauses the schedule after 60 days without
any activity in the repository.
