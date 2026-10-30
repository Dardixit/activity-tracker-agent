"""Offline: a tap on every kind of question button reaches its question.

Calls bot.send with Telegram faked, reads the link off the button the way
Telegram does (deep linking allows A-Z a-z 0-9 _ - and 64 characters), and
feeds the resulting /start message through bot.handle_update. Also checks that
questions sent with callback buttons are swapped for links in place, once, and
that a tap on a closed question says so. Writes nothing to any Sheet.

Run with:  .venv/bin/python test_taps.py
"""

import json
import re
import tempfile
from pathlib import Path

import bot
import calendar_sync

CHAT = bot.ALLOWED_CHAT_ID
PERSON = {"kind": "note", "pending": {"person": "Anna"}, "person": "Anna", "duplicate": None, "text": "Same Anna?"}
DUPLICATE = {"kind": "note", "pending": {"duplicate": 5}, "person": None, "duplicate": 5, "text": "Same row?"}
PLAIN_NOTE = {"kind": "note", "pending": {}}
CONFIRM = {"kind": "confirm", "text": "1 yes, 2 no", "group": "g", "question": "Answers?"}
CALENDAR = [{"kind": kind, "title": "x"} for kind in (*calendar_sync.BUTTONS, "attend", "title", "cancel")]


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeTelegram:
    def __init__(self):
        self.next_id, self.buttons, self.calls = 100, {}, []

    def post(self, url, data=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, data))
        if method == "sendMessage":
            self.next_id += 1
            return FakeResponse({"ok": True, "result": {"message_id": self.next_id}})
        if method == "editMessageReplyMarkup":
            rows = json.loads(data.get("reply_markup") or '{"inline_keyboard": []}')["inline_keyboard"]
            self.buttons[data["message_id"]] = [b for row in rows for b in row]
        return FakeResponse({"ok": True, "result": True})


def main():
    results = []

    def check(ok, text):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'} {text}")

    fake = FakeTelegram()
    bot.requests = fake
    bot.PENDING_FILE = Path(tempfile.mkdtemp()) / "pending.json"
    bot.telegram_on, bot.bot_username = True, "test_bot"
    bot.state.BACKEND = "files"

    # Every question is answered by what its own buttons send.
    answered = []
    real_answer_question = bot.answer_question
    bot.answer_question = lambda c, w, chat, qid, q, text, choice=None: (answered.append((qid, choice)), ("done", None))[1]
    bot.calendar_sync.answer = lambda c, w, items, text, answers=None, numbers=None: (
        answered.append((items[0]["kind"], answers)), ("Answers recorded.", []))[1]
    bot.answer_group = lambda *a, **k: (answered.append(("group", a[3])), "done")[1]
    bot.after_calendar_answers = lambda *a: None
    bot.agent.load_sheet = lambda: {}
    bot.agent.start = lambda client, data, text: answered.append(("note", text))
    real_finish = bot.finish
    bot.finish = lambda writer, data, run: ("done", None)

    questions = [PERSON, DUPLICATE, CONFIRM] + [
        {"kind": "calendar", "items": [item], "group": "g", "number": 1, "of": 1, "text": "?"} for item in CALENDAR]
    for question in questions:
        mid = bot.send(CHAT, "question", question=dict(question))
        expected = bot.question_buttons(question)
        links = fake.buttons.get(mid, [])
        check(len(links) == len(expected) and all("callback_data" not in b for b in links),
              f"{question.get('person') or question['kind']} {question.get('items', [{}])[0].get('kind', '')}: {len(links)} link buttons")
        for button, (_, value) in zip(links, expected):
            start = button["url"].split("?start=", 1)[1]
            check(bool(re.fullmatch(r"[\w-]{1,64}", start)), f"  {start}: a valid start parameter")
            again = dict(question)
            bot.save_questions({**bot.load_questions(), str(mid): again})
            count = len(answered)
            bot.handle_update(None, None, {"message": {"chat": {"id": CHAT}, "text": "/start " + start}})
            check(len(answered) == count + 1, f"  {value}: reached its question")

    # The tap you make: Same person, new row.
    fake2 = FakeTelegram()
    bot.requests = fake2
    bot.save_questions({})
    bot.answer_question, bot.finish = real_answer_question, real_finish
    resumed = []
    bot.agent.continue_run = lambda client, data, pending, text, choice: (resumed.append((text, choice)), {
        "asked": False, "problem": None, "proposals": [], "final_text": "ok"})[1]
    bot.okr.recount = lambda writer: ([], [])
    mid = bot.send(CHAT, "question", question=dict(PERSON))
    link = next(b for b in fake2.buttons[mid] if b["text"] == "Same person, new row")
    bot.handle_update(None, None, {"message": {"chat": {"id": CHAT}, "text": "/start " + link["url"].split("?start=")[1]}})
    check(resumed == [("Same person, new row", "same_new")], f"the paused run resumes with same_new: {resumed}")
    check(str(mid) not in bot.load_questions(), "the question is closed")

    # Questions sent before this change, with callback buttons.
    fake3 = FakeTelegram()
    bot.requests = fake3
    bot.save_questions({"7": dict(PERSON), "8": dict(PLAIN_NOTE), "9": {
        "kind": "calendar", "items": [CALENDAR[0]], "group": "g", "number": 1, "of": 1, "text": "?"}})
    bot.migrate_buttons(CHAT)
    check(sorted(fake3.buttons) == [7, 9], "old questions with buttons got links in place; a plain question was left alone")
    check(not any(c[0] == "sendMessage" for c in fake3.calls), "nothing was sent again")
    calls = len(fake3.calls)
    bot.migrate_buttons(CHAT)
    check(len(fake3.calls) == calls, "the swap happens once")

    # A tap on a question that is not open.
    bot.handle_update(None, None, {"message": {"chat": {"id": CHAT}, "text": "/start q55_yes"}})
    check(fake3.buttons.get(55) == [], "a tap on a closed question takes its buttons away")
    check(any("not open any more" in c[1].get("text", "") for c in fake3.calls), "and says nothing was changed")

    # /start is never a note or an answer, whatever follows it.
    bot.agent.start = bot.agent.continue_run = lambda *a, **k: results.append(False)
    for text in ("/start", "/start probe_new", "/START", "/start@test_bot x", " /start q12", "/start q7_bad value"):
        calls = len(fake3.calls)
        bot.handle_update(None, None, {"message": {"chat": {"id": CHAT}, "text": text}})
        check("not a note" in fake3.calls[-1][1].get("text", "") and len(fake3.calls) == calls + 1, f"{text!r} is not a note")
    calls = len(fake3.calls)
    bot.handle_update(None, None, {"message": {"chat": {"id": CHAT}, "text": "/start again",
                                               "reply_to_message": {"message_id": 7}}})
    check("7" in bot.load_questions(), "/start as a reply does not answer the question")

    # Not from you: ignored, and not a note.
    calls = len(fake3.calls)
    bot.handle_update(None, None, {"message": {"chat": {"id": CHAT + 1}, "text": "/start q7_same_new"}})
    check(len(fake3.calls) == calls and "7" in bot.load_questions(), "a tap from another chat is ignored")

    print(f"\n{sum(results)}/{len(results)} checks passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
