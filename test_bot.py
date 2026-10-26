"""Send test messages through the live bot's own code, with only Telegram faked.

This runs bot.main() itself: the getUpdates loop, the chat filter, handle(),
the writer and send() are the same code the live bot runs. Only the Telegram
servers are replaced: each test message arrives as an update from your chat,
and replies are printed here instead of being sent.

A test message starting with "reply:" is sent as a Telegram reply to the
latest question the bot asked, e.g.  "reply: yes, 26.10 is right";
"reply:3: same row" replies to calendar question 3 of the latest set.
"tap:3:no" taps a button (its value: yes/no/skip) on calendar question 3, and
"tap:confirm:answers" / "tap:confirm:note" taps the "is this a list of answers?"
question, and "tap:note:same_new" (same_update/same_new/different, or
dup_same/dup_different/dup_skip) taps a button on the latest same-name or
duplicate question. --test-questions first sends four fake calendar questions (titles
starting with "test:"), so buttons can be tested without real events.

Writes go to the target Sheet like real messages, but every Changes row they
produce has "test: " in front of its reason. The Telegram offset and open
test questions are kept in their own files, so the live bot's are never touched.
The test files are used whatever STATE_BACKEND says, so a test never touches
the Agent state tab either. The calendar check is off unless --calendar is
given. --once runs the bot's one-shot mode (bot.py --once, as on GitHub
Actions) instead of the polling loop: all test messages are waiting at once.
At the end, every message that was sent is listed.

Run with:  .venv/bin/python test_bot.py "first test message" "reply: an answer"
"""

import json
import sys
from pathlib import Path

import bot

# Fake calendar questions for --test-questions. They name no real event or
# row, so answering them can only write rows in the Calendar titles tab.
TEST_QUESTIONS = [
    {"kind": "title", "title": "test: Alpha meetup", "count": 1, "date": "01.12.2026"},
    {"kind": "title", "title": "test: Beta workshop", "count": 2, "date": "02.12.2026"},
    {"kind": "title", "title": "test: Gamma webinar", "count": 1, "date": "03.12.2026"},
    {"kind": "title", "title": "test: Delta talk", "count": 1, "date": "04.12.2026"},
]


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeTelegram:
    """Stands in for the requests module inside bot.py."""

    def __init__(self, messages, once=False):
        # One update per poll, like messages sent a few seconds apart.
        self.messages = list(messages)
        self.once = once
        self.next_update = 900000
        self.next_message_id = 500000
        self.last_question = None  # id of the latest bot message that was stored as a question
        self.by_number = {}        # calendar question number -> message id (latest set)
        self.last_confirm = None

    def get(self, url, params=None, timeout=None):
        if url.endswith("/getMe"):
            return FakeResponse({"ok": True, "result": {"username": "test-bot (Telegram faked)"}})
        if url.endswith("/getUpdates"):
            if not self.messages:
                if self.once:
                    return FakeResponse({"ok": True, "result": []})  # nothing more waiting
                raise KeyboardInterrupt  # every test message handled: stop the bot loop
            text = self.messages.pop(0)
            self.next_update += 1
            if text.startswith("tap:"):
                _, target, value = text.split(":", 2)
                mid = {"confirm": self.last_confirm, "note": self.last_question}.get(target) \
                    or self.by_number[int(target)]
                print("\n" + "=" * 78 + f"\nTEST TAP: button {value!r} on message {mid}\n" + "=" * 78)
                me = {"id": bot.ALLOWED_CHAT_ID}
                callback = {"id": f"cb{self.next_update}", "from": me, "data": value,
                            "message": {"message_id": mid, "chat": {"id": bot.ALLOWED_CHAT_ID}}}
                return FakeResponse({"ok": True, "result": [{"update_id": self.next_update, "callback_query": callback}]})
            message = {"chat": {"id": bot.ALLOWED_CHAT_ID}, "text": text}
            if text.startswith("reply:"):
                rest = text[len("reply:"):]
                target = self.last_question
                head, sep, tail = rest.partition(":")
                if sep and head.strip().isdigit():
                    target, rest = self.by_number[int(head)], tail
                message["text"] = rest.strip()
                message["reply_to_message"] = {"message_id": target}
            label = f"(reply to message {message['reply_to_message']['message_id']}) " if "reply_to_message" in message else ""
            print("\n" + "=" * 78 + f"\nTEST MESSAGE {label}: {message['text']}\n" + "=" * 78)
            return FakeResponse({"ok": True, "result": [{"update_id": self.next_update, "message": message}]})
        raise ValueError(f"Unexpected GET {url}")

    def post(self, url, data=None, timeout=None):
        if url.endswith("/sendMessage"):
            self.next_message_id += 1
            buttons = ""
            if data.get("reply_markup"):
                rows = json.loads(data["reply_markup"])["inline_keyboard"]
                buttons = "\n" + "  ".join(f"[{b['text']}]" for row in rows for b in row)
            print(f"\n######## TELEGRAM REPLY (message {self.next_message_id}) ########\n{data['text']}{buttons}\n"
                  "################################")
            return FakeResponse({"ok": True, "result": {"message_id": self.next_message_id}})
        if url.endswith("/editMessageText"):
            print(f"\n######## EDITED message {data['message_id']} (buttons removed) ########\n{data['text']}\n"
                  "################################")
        return FakeResponse({"ok": True})


def main(args):
    calendar = "--calendar" in args
    test_questions = "--test-questions" in args
    once = "--once" in args
    messages = [a for a in args if a not in ("--calendar", "--test-questions", "--once")]
    if not messages and not calendar:
        sys.exit(__doc__)
    here = Path(__file__).parent
    bot.OFFSET_FILE = here / "last_update_id.test.txt"
    bot.PENDING_FILE = here / "pending_questions.test.json"
    bot.state.BACKEND = "files"
    # Telegram is faked, so it belongs to this test's Sheet (also for --test-questions, sent before main()).
    bot.TELEGRAM_SHEET_TARGET = bot.agent.sheets.SHEET_TARGET
    bot.telegram_on = True
    telegram = bot.requests = FakeTelegram(messages, once)
    real_send = bot.send

    def send(chat_id, text, question=None, buttons=None):
        message_id = real_send(chat_id, text, question, buttons)
        if question is not None:
            telegram.last_question = message_id
            if question["kind"] == "calendar" and question.get("number"):
                if question["number"] == 1:
                    telegram.by_number = {}
                telegram.by_number[question["number"]] = message_id
            if question["kind"] == "confirm":
                telegram.last_confirm = message_id
        return message_id

    bot.send = send
    if test_questions:
        bot.send_calendar_questions(bot.ALLOWED_CHAT_ID, TEST_QUESTIONS)
    try:
        status = bot.main(reason_prefix="test: ", calendar=calendar, once=once, force=True)
        if once:
            print(f"(One-shot run exit status: {status})")
    except KeyboardInterrupt:
        pass
    finally:
        bot.OFFSET_FILE.unlink(missing_ok=True)

    print("\nTest messages sent:")
    for i, message in enumerate(messages, 1):
        print(f"  {i}. {message}")
    if bot.load_questions():
        print("(A test question is still open; send /skip as a test message to drop it.)")


if __name__ == "__main__":
    main(sys.argv[1:])
