"""Move the bot's local state into the Agent state tab, once, when GitHub
Actions takes over from a bot running on your own machine.

Copies last_update_id.txt, pending_questions.json and calendar_state.json
into the Agent state tab of the target Sheet (SHEET_TARGET), and sets
runner = "github-actions", after which bot.py refuses to poll locally.
Stop the local bot first. The local files are left as they are.
Refuses if the tab already holds a Telegram offset, unless --force is given.

Run with:  .venv/bin/python migrate_state.py           (add --force to overwrite)
"""

import json
import sys

import bot
import calendar_sync
import state


def main(args):
    store = state.SheetStore()
    if store.get("telegram_offset") is not None and "--force" not in args:
        sys.exit(f"The {state.TAB} tab already holds Telegram offset {store.get('telegram_offset')}. "
                 "Nothing was copied; use --force to overwrite.")

    offset = int(bot.OFFSET_FILE.read_text().strip() or 0) if bot.OFFSET_FILE.exists() else 0
    questions = json.loads(bot.PENDING_FILE.read_text()) if bot.PENDING_FILE.exists() else {}
    calendar = json.loads(calendar_sync.STATE_FILE.read_text()) if calendar_sync.STATE_FILE.exists() else {}

    old_questions = {k: None for k in store.items("question:")}
    store.put({
        **old_questions,
        "telegram_offset": offset,
        **{f"question:{mid}": q for mid, q in questions.items()},
        "calendar_state": calendar,
        "runner": "github-actions",
    })
    print(f"Copied to the {state.TAB} tab of the {state.sheets.SHEET_TARGET} Sheet:")
    print(f"  telegram_offset: {offset}")
    print(f"  open questions: {len(questions)}")
    print(f"  calendar_state: {len(calendar.get('ignore_cancel', []))} ignored cancellation(s), "
          f"{len(calendar.get('ignore_event', []))} ignored event(s), {len(calendar.get('snoozed', {}))} snoozed")
    print("  runner: github-actions (bot.py now refuses to poll locally)")


if __name__ == "__main__":
    main(sys.argv[1:])
