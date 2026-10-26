"""Three one-shot runs on the test Sheet, as on GitHub Actions, with only Telegram faked.

Each run is its own process running bot.main(once=True) with the Agent state
tab of the test Sheet (STATE_BACKEND=sheet), the calendar check on, the real
calendar and Claude. Nothing survives between runs except what is saved in the
Sheet, as on GitHub Actions. The fake Telegram keeps its waiting updates and
sent messages in telegram_fake.test.json between runs, and drops updates once
a run confirms them (getUpdates with a higher offset), as Telegram does.

  run 1      asks questions (calendar check)
  (between)  your taps wait in Telegram: every question gets a button tap
             (attend: yes, cancel: no, the rest: skip), plus one tap on a
             message that is no question
  run 1b     a run on the other Sheet (TELEGRAM_SHEET_TARGET=real): must not
             fetch the waiting taps or send anything
  run 2      gets the taps: each is applied, its question closed in the
             Agent state tab; the stray tap is answered "not open any more"
  run 3      asks nothing that runs 1 and 2 asked
  run 4      the backup schedule in the same hour (--scheduled): does nothing
             (outside 01:00-06:59 Helsinki time)

Writes go to the test Sheet only, labelled "test: "; undo them with
revert.py --test. Calls Claude (calendar decisions).

Run with:  .venv/bin/python test_runs.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
FAKE_FILE = HERE / "telegram_fake.test.json"
FIRST_MESSAGE_ID = 900000  # fake message ids start above the real ones
STRAY_MESSAGE = 1  # a message id no question has
TAP_VALUE = {"attend": "yes", "cancel": "no"}  # everything else: skip


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class PersistentTelegram:
    """Stands in for the requests module inside bot.py, with state in FAKE_FILE."""

    def __init__(self, run):
        self.run = run
        self.s = json.loads(FAKE_FILE.read_text())

    def save(self):
        FAKE_FILE.write_text(json.dumps(self.s, ensure_ascii=False, indent=1))

    def get(self, url, params=None, timeout=None):
        if url.endswith("/getMe"):
            return FakeResponse({"ok": True, "result": {"username": "test-bot (Telegram faked)"}})
        if url.endswith("/getUpdates"):
            self.s["calls"].append({"run": self.run, "method": "getUpdates", "offset": params["offset"]})
            # A higher offset confirms the updates below it: Telegram deletes them.
            self.s["waiting"] = [u for u in self.s["waiting"] if u["update_id"] >= params["offset"]]
            self.save()
            return FakeResponse({"ok": True, "result": self.s["waiting"]})
        raise ValueError(f"Unexpected GET {url}")

    def post(self, url, data=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.s["calls"].append({"run": self.run, "method": method, **{k: data.get(k) for k in ("message_id", "text")}})
        result = True
        if method == "sendMessage":
            self.s["next_message_id"] += 1
            result = {"message_id": self.s["next_message_id"]}
            self.s["sent"].append({"run": self.run, "message_id": self.s["next_message_id"], "text": data["text"],
                                   "buttons": bool(data.get("reply_markup"))})
        self.save()
        return FakeResponse({"ok": True, "result": result})


def one_run(run, telegram_sheet, scheduled=False):
    """Inside a child process: one bot.py --once run with the fake Telegram."""
    os.environ.update(STATE_BACKEND="sheet", SHEET_TARGET="test", TELEGRAM_SHEET_TARGET=telegram_sheet)
    import bot
    if bot.agent.sheets.SHEET_TARGET != "test":
        sys.exit("test_runs.py works on the test Sheet only.")
    bot.requests = PersistentTelegram(run)
    return bot.main(reason_prefix="test: ", calendar=True, once=True, scheduled=scheduled)


def child(run, telegram_sheet="test", scheduled=False):
    print(f"\n{'=' * 30} run {run} (Telegram belongs to the {telegram_sheet} Sheet) {'=' * 30}", flush=True)
    status = subprocess.run([sys.executable, __file__, "--run", run, telegram_sheet]
                            + (["--scheduled"] if scheduled else []), cwd=HERE).returncode
    return status, json.loads(FAKE_FILE.read_text())


def open_questions():
    """The open questions as saved in the test Sheet's Agent state tab, read fresh."""
    os.environ.update(STATE_BACKEND="sheet", SHEET_TARGET="test")
    import calendar_sync
    import state
    store = state.SheetStore()
    found = {}
    for key, q in store.items("question:").items():
        mid = int(key.split(":", 1)[1])
        found[mid] = (q["kind"], calendar_sync.item_key(q["items"][0]) if q["kind"] == "calendar" else None, q)
    return found, store.get("calendar_state", {})


def clear_fake_questions():
    """Close questions an earlier test_runs.py left open (fake message ids), and
    return the saved Telegram offset: fake updates must be numbered above it."""
    os.environ.update(STATE_BACKEND="sheet", SHEET_TARGET="test")
    import state
    store = state.SheetStore()
    store.put({k: None for k in store.items("question:") if int(k.split(":", 1)[1]) >= FIRST_MESSAGE_ID})
    return int(store.get("telegram_offset", 0))


def main():
    offset = clear_fake_questions()
    FAKE_FILE.write_text(json.dumps({"next_message_id": FIRST_MESSAGE_ID, "next_update": offset + 1000,
                                     "waiting": [], "sent": [], "calls": []}))
    results = []

    def check(ok, text):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'} {text}")

    before, _ = open_questions()
    status, s = child("1")
    check(status == 0, f"run 1 exit status {status}")
    asked, state_cal = open_questions()
    new = {mid: v for mid, v in asked.items() if mid not in before}
    sent_ids = {m["message_id"] for m in s["sent"] if m["run"] == "1"}
    check(bool(new), f"run 1 asked {len(new)} question(s), saved in the Agent state tab")
    check(set(new) <= sent_ids, "every saved question was sent as a Telegram message")
    if not new:
        return finish(results)
    keys_asked = {v[1] for v in asked.values() if v[1]}

    # Your taps, waiting in Telegram until a run fetches them.
    chat = int(os.environ.get("TELEGRAM_CHAT_ID") or __import__("dotenv").dotenv_values(HERE / ".env")["TELEGRAM_CHAT_ID"])
    taps = {}
    for mid, (kind, key, q) in sorted(new.items()):
        value = TAP_VALUE.get(q["items"][0]["kind"], "skip") if kind == "calendar" else "dup_skip"
        taps[mid] = (value, key)
    for mid in list(taps) + [STRAY_MESSAGE]:
        s["next_update"] += 1
        value = taps[mid][0] if mid in taps else "yes"
        s["waiting"].append({"update_id": s["next_update"], "callback_query": {
            "id": f"cb{s['next_update']}", "from": {"id": chat}, "data": value,
            "message": {"message_id": mid, "chat": {"id": chat}, "text": "an old question"}}})
    FAKE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    print("\nTaps waiting in Telegram: " + ", ".join(f"{v[0]} on {mid} ({v[1]})" for mid, v in taps.items())
          + f", yes on message {STRAY_MESSAGE} (no question)")

    status, s = child("1b", telegram_sheet="real")
    calls = [c for c in s["calls"] if c["run"] == "1b"]
    check(status == 0, f"run 1b exit status {status}")
    check(not any(c["method"] == "getUpdates" for c in calls), "run 1b (other Sheet) fetched no updates")
    check(not any(c["method"] == "sendMessage" for c in calls), "run 1b (other Sheet) sent nothing")
    check(len(s["waiting"]) == len(taps) + 1, f"all {len(taps) + 1} taps still waiting after run 1b")

    status, s = child("2")
    check(status == 0, f"run 2 exit status {status}")
    check(not s["waiting"], "run 2 fetched and confirmed every waiting tap")
    after, state_cal = open_questions()
    check(not set(taps) & set(after), "every tapped question is closed in the Agent state tab")
    for mid, (value, key) in taps.items():
        if value == "skip":
            check(key in state_cal.get("snoozed", {}), f"skip on {key}: snoozed in calendar_state")
        if value == "no" and key.startswith("cancel:"):
            check(key.split(":", 1)[1] in state_cal.get("ignore_cancel", []), f"no on {key}: in ignore_cancel")
    run2 = [c for c in s["calls"] if c["run"] == "2"]
    check(any(c["method"] == "editMessageText" and c["message_id"] == STRAY_MESSAGE for c in run2),
          "the tap on a message with no question took its buttons away")
    check(any(c["method"] == "sendMessage" and "not open any more" in (c["text"] or "") for c in run2),
          "the tap on a message with no question was answered with a message")
    keys_asked |= {v[1] for v in after.values() if v[1]}

    status, s = child("3")
    check(status == 0, f"run 3 exit status {status}")
    final, _ = open_questions()
    again = [v[1] for mid, v in final.items() if mid not in after and v[1] in keys_asked]
    check(not again, "run 3 asked nothing that runs 1 and 2 asked" + (f" (asked again: {again})" if again else ""))
    new3 = [v[1] or v[0] for mid, v in final.items() if mid not in after]
    print(f"Run 3 asked {len(new3)} new question(s){': ' + ', '.join(new3) if new3 else ''}")

    status, s = child("4", scheduled=True)
    calls = [c for c in s["calls"] if c["run"] == "4"]
    check(status == 0 and not calls, "run 4 (backup schedule, same hour) did nothing: no Telegram calls")
    return finish(results)


def finish(results):
    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--run"]:
        sys.exit(one_run(sys.argv[2], sys.argv[3], scheduled="--scheduled" in sys.argv))
    sys.exit(main())
