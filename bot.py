"""Telegram bot: each message you send runs the agent, which writes the change
to the Sheet (logged in the Changes tab) and replies with what it changed.

If the note conflicts with the Sheet, the bot asks instead and writes nothing.
Answer a question by replying to it in Telegram (reply /skip to drop it). Before
a new row names a person whose first name is already in the Sheet, the bot asks
whether it is the same person, with buttons. Any
message that is not a reply to an open question is a new note, except a
numbered list of answers while calendar questions are open: the bot asks
whether it answers them.

Calendar questions come one per message with buttons; a tap answers exactly
that question, and the message then shows the answer instead of the buttons.
After every run that isn't a question, the OKRs tab is recounted.

With CALENDAR_SYNC=on, the Google Calendar is checked at start and then every
hour (calendar_sync.py); its questions arrive as one numbered message.

Writes go to the Sheet chosen by SHEET_TARGET in .env (the test copy unless set to "real").
Only messages from TELEGRAM_CHAT_ID are handled; everyone else is ignored.
Network errors (Telegram, Google, Claude) never stop the bot: they are logged
in one line and the call is tried again, waiting longer each time (retry.py).

Run with:  .venv/bin/python bot.py          (keeps polling; Ctrl+C to stop)
      or:  .venv/bin/python bot.py --once   (one-shot, for GitHub Actions: handle
           what is waiting, run the calendar check and the OKR recount, exit;
           exits with status 1 if anything went wrong)
Options:   --reason-prefix "test: "         (put in front of every Changes reason)
           --force                          (poll even if the Agent state tab says
                                             GitHub Actions runs the bot)

Where the Telegram offset, open questions and calendar state live is set by
STATE_BACKEND (state.py): local files by default, the Agent state tab with "sheet".
"""

import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv

import agent
import calendar_sync
import okr
import retry
import sheet_writer
import state

# Load the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID variables out of the .env file.
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if not TOKEN:
    sys.exit(
        "No token found.\n"
        "Open the .env file and paste your token after TELEGRAM_BOT_TOKEN=\n"
        "You get the token from @BotFather in Telegram."
    )

# Without this the bot would answer anyone who finds it, so refuse to start.
if not ALLOWED_CHAT_ID.lstrip("-").isdigit():
    sys.exit(
        "TELEGRAM_CHAT_ID is missing or not a number in the .env file.\n"
        "Set it to your own chat ID; messages from every other chat are ignored."
    )
ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID)

API = f"https://api.telegram.org/bot{TOKEN}"

# Telegram rejects messages longer than this.
TELEGRAM_MAX_LEN = 4096

# Telegram gives every update a number. We store the last one we handled so
# that we never handle the same message twice, even after a restart.
OFFSET_FILE = Path(__file__).with_name("last_update_id.txt")

# Open questions, by the Telegram message id of the question, so a reply or a
# button tap can answer it. (Ignored by git: *.json.)
#   {"kind": "note", "pending": <paused agent run>}
#     plus "person" (first name) and "text" (message text) for a same-name
#     question, which has the agent.PERSON_CHOICES buttons
#   {"kind": "calendar", "items": [item], "group": id, "number": n, "of": total, "text": message text}
#   {"kind": "confirm", "text": your numbered list, "group": id, "question": message text}
# Calendar questions sent together share a group; "number" is their n of total.
PENDING_FILE = Path(__file__).with_name("pending_questions.json")

# The calendar check runs at start and then this often.
CALENDAR_EVERY = 3600
CALENDAR_SYNC = os.getenv("CALENDAR_SYNC", "").strip().lower() == "on"
calendar_active = False  # set by main(): whether this bot runs calendar checks
# What went wrong in this run (handled errors included), so --once can exit
# with status 1 and GitHub Actions reports the run as failed.
run_errors = []

HERE = Path(__file__).parent


def code_stamp():
    """When each .py file in the project was last changed."""
    return {p.name: p.stat().st_mtime for p in HERE.glob("*.py")}


# Python loads the code once, at start. A bot left running while the code
# changes would keep writing the old way, so it restarts itself instead.
STARTED_WITH = code_stamp()


def restart_if_code_changed():
    if code_stamp() != STARTED_WITH:
        print("\nThe code changed on disk since the bot started. Restarting to load it.")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def code_version():
    """The git commit the bot runs, e.g. "47d81a5", plus "+ uncommitted changes" if any."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "*.py"], cwd=HERE,
                               capture_output=True, text=True).stdout.strip()
        return commit + (" + uncommitted changes" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_offset():
    if state.in_sheet():
        return int(state.store().get("telegram_offset", 0))
    if OFFSET_FILE.exists():
        return int(OFFSET_FILE.read_text().strip() or 0)
    return 0


def save_offset(update_id):
    if state.in_sheet():
        state.store().put({"telegram_offset": update_id})
        return
    OFFSET_FILE.write_text(str(update_id))


def load_questions():
    if state.in_sheet():
        return {k.split(":", 1)[1]: q for k, q in state.store().items("question:").items()}
    if PENDING_FILE.exists():
        return json.loads(PENDING_FILE.read_text())
    return {}


def save_questions(questions):
    if state.in_sheet():
        # One row per question: write the new and changed ones, blank the answered ones.
        old = state.store().items("question:")
        new = {f"question:{mid}": q for mid, q in questions.items()}
        state.store().put({**{k: None for k in old if k not in new}, **new})
        return
    PENDING_FILE.write_text(json.dumps(questions, ensure_ascii=False))


def open_calendar_keys():
    """Keys of calendar questions still waiting for an answer, so they aren't asked twice."""
    return {calendar_sync.item_key(item) for q in load_questions().values()
            if q["kind"] == "calendar" for item in q["items"]}


def open_group(questions):
    """The latest group of calendar questions with any still open: (group, {message id: question})."""
    groups = {}
    for mid, q in questions.items():
        if q["kind"] == "calendar" and q.get("group"):
            groups.setdefault(q["group"], {})[mid] = q
    if not groups:
        return None, {}
    latest = max(groups)
    return latest, groups[latest]


def plain(text):
    """Strip markdown the model may still write, since replies are sent as plain text.

    Removes **bold** / __bold__ markers, `backticks` and leading # heading marks.
    Single * and _ are left alone: they appear in names and links.
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.S)
    text = text.replace("`", "")
    return re.sub(r"(?m)^#{1,6}\s+", "", text)


def keyboard(button_list):
    """Telegram inline keyboard: one row of (label, answer) buttons."""
    return json.dumps({"inline_keyboard": [[{"text": label, "callback_data": value} for label, value in button_list]]})


def telegram(method, data, attempts=retry.ATTEMPTS):
    """POST to the Telegram API, trying again after network errors. Returns
    Telegram's reply, or {"ok": False, ...} if Telegram could not be reached."""
    try:
        return retry.call(f"Telegram {method}",
                          lambda: requests.post(f"{API}/{method}", data=data, timeout=30).json(), attempts)
    except Exception as err:
        if not retry.is_network_error(err):
            raise
        return {"ok": False, "description": f"Telegram could not be reached ({retry.describe(err)})"}


def send(chat_id, text, question=None, buttons=None):
    """Send a message, with inline buttons if given. If `question` is given, it
    is stored as an open question under the sent message's id, so a reply or a
    button tap answers it. A same-name question gets its buttons here.
    Returns the message id (None if sending failed)."""
    if buttons is None and question and question.get("person"):
        buttons = [(label, value) for value, label, _ in agent.PERSON_CHOICES]
    text = plain(text)
    if len(text) > TELEGRAM_MAX_LEN:
        text = text[: TELEGRAM_MAX_LEN - 20] + "\n\n[...cut off]"
    data = {"chat_id": chat_id, "text": text}
    if buttons:
        data["reply_markup"] = keyboard(buttons)
    reply = telegram("sendMessage", data)
    if not reply.get("ok"):
        print(f"Could not send reply: {reply.get('description')}")
        run_errors.append(f"Telegram sendMessage: {reply.get('description')}")
        return None
    message_id = reply["result"]["message_id"]
    if question is not None:
        questions = load_questions()
        questions[str(message_id)] = question
        save_questions(questions)
    return message_id


def edit(chat_id, message_id, text):
    """Replace a sent message's text; this also removes its buttons."""
    reply = telegram("editMessageText", {
        "chat_id": chat_id, "message_id": message_id, "text": plain(text)[:TELEGRAM_MAX_LEN]})
    if not reply.get("ok"):
        print(f"Could not edit message {message_id}: {reply.get('description')}")


def acknowledge(callback_id, text=""):
    """Tell Telegram a button tap was handled (stops the loading spinner)."""
    # One try: the tap is handled either way, only the spinner stays longer.
    reply = telegram("answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, attempts=1)
    if not reply.get("ok"):
        print(f"Could not acknowledge a button tap: {reply.get('description')}")


def send_calendar_questions(chat_id, items):
    """Send calendar questions one per message, each with its own buttons."""
    group = agent.now().strftime("%Y%m%d%H%M%S")
    for n, item in enumerate(items, 1):
        text = calendar_sync.single_question(item, n, len(items))
        send(chat_id, text, buttons=calendar_sync.buttons(item), question={
            "kind": "calendar", "items": [item], "group": group, "number": n, "of": len(items), "text": text})


def migrate_batches(chat_id):
    """Old-style messages holding several calendar questions: send their open
    questions again one per message with buttons, and retire the old message."""
    questions = load_questions()
    old = {mid: q for mid, q in questions.items() if q["kind"] == "calendar" and "group" not in q}
    for mid, q in old.items():
        questions.pop(mid)
        save_questions(questions)
        edit(chat_id, int(mid), "These calendar questions are now sent again below, one per message with buttons.")
        send_calendar_questions(chat_id, q["items"])
        questions = load_questions()


def show_typing(chat_id):
    # The agent takes a while; this shows "typing..." in Telegram meanwhile.
    reply = telegram("sendChatAction", {"chat_id": chat_id, "action": "typing"}, attempts=1)
    if not reply.get("ok"):
        print(f"Could not show typing: {reply.get('description')}")


def finish(writer, data, run):
    """Write what an agent run decided and recount the OKRs.

    Returns (reply text, question to store or None)."""
    if run["asked"]:
        text = "Question before I change anything (nothing was written):\n\n" + run["question"]
        if run["person"]:
            text += "\n\nTap a button, or reply to this message in words, or reply /skip to drop the question."
            return text, {"kind": "note", "pending": run["pending"], "person": run["person"], "text": text}
        text += "\n\nReply to this message with your answer, or reply /skip to drop the question."
        return text, {"kind": "note", "pending": run["pending"]}
    if run["problem"]:
        return "Nothing was written: " + run["problem"], None

    if run["proposals"]:
        edits, inserts = sheet_writer.edits_from_proposals(data, run["proposals"])
        parts = [sheet_writer.describe(edits, inserts, writer.apply(edits, inserts))]
    else:
        parts = ["No change made.\n\n" + (run["final_text"] or "The Sheet already covers this.")]

    # The change above is already written, so a problem here must not be
    # reported as "nothing was written".
    try:
        okr_edits, logged = okr.recount(writer)
        if okr_edits:
            parts.append(okr.describe(okr_edits) + "\n" + sheet_writer.logged_text(logged))
    except sheet_writer.WriteRefused as err:
        parts.append(f"OKR recount skipped: {err}")
    return "\n\n".join(parts), None


def answer_question(client, writer, chat_id, question_id, question, text, choice=None):
    """A reply to an open question (or a tap: `choice` is the button value).
    Returns (reply text, question to store or None)."""
    questions = load_questions()
    if text.strip().lower() == "/skip":
        questions.pop(question_id, None)
        save_questions(questions)
        return "Dropped that question. Nothing was changed.", None

    if question["kind"] == "note":
        # Read the Sheet fresh each time, so manual edits are picked up.
        data = agent.load_sheet()
        run = agent.continue_run(client, data, question["pending"], text, choice)
        reply, new_question = finish(writer, data, run)
        questions = load_questions()
        questions.pop(question_id, None)  # answered (a follow-up question is stored under its own message)
        save_questions(questions)
        return reply, new_question

    if question["kind"] == "confirm":
        return "Please use the buttons on that message: Yes, these are answers / No, it's a note.", None

    # A calendar question answered in words.
    reply, remaining = calendar_sync.answer(client, writer, question["items"], text)
    if not remaining:
        close_calendar_question(chat_id, question_id, question, f"Answered: {text}")
    after_calendar_answers(client, writer, chat_id)
    return reply, None


def close_calendar_question(chat_id, question_id, question, shown):
    """Remove an answered calendar question and show the answer in its message."""
    questions = load_questions()
    questions.pop(question_id, None)
    save_questions(questions)
    if question.get("text"):
        edit(chat_id, int(question_id), f"{question['text']}\n\n{shown}")


def after_calendar_answers(client, writer, chat_id):
    # Titles you said yes to can be added right away.
    if calendar_active:
        run_calendar(client, writer, chat_id)


def answer_group(client, writer, chat_id, group, text, answers=None):
    """A numbered list of answers for a group of calendar questions."""
    questions = {mid: q for mid, q in open_group(load_questions())[1].items()}
    if not questions or next(iter(questions.values()))["group"] != group:
        return "Those calendar questions are no longer open."
    ordered = sorted(questions.items(), key=lambda kv: kv[1]["number"])
    items = [q["items"][0] for _, q in ordered]
    numbers = [q["number"] for _, q in ordered]
    if answers is None:
        answers = calendar_sync.read_answers(client, items, text, numbers)
    reply, remaining = calendar_sync.answer(client, writer, items, text, answers=answers, numbers=numbers)
    for (mid, q), item, n in zip(ordered, items, numbers):
        if item not in remaining:
            close_calendar_question(chat_id, mid, q, "Your answer (numbered list): "
                                    + calendar_sync.answer_label(item, answers[n]))
    after_calendar_answers(client, writer, chat_id)
    return reply


def handle_tap(client, writer, callback):
    """A button tap on one of the bot's messages."""
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    question_id = str(message.get("message_id", ""))
    value = callback.get("data", "")
    question = load_questions().get(question_id)
    if question is None:
        acknowledge(callback["id"], "This question is already answered.")
        return
    print(f"\nButton {value!r} on question {question_id}")
    acknowledge(callback["id"])
    try:
        if question["kind"] == "confirm":
            questions = load_questions()
            questions.pop(question_id, None)
            save_questions(questions)
            if value == "answers":
                edit(chat_id, int(question_id), f"{question['question']}\n\nYes, these are answers.")
                reply = answer_group(client, writer, chat_id, question["group"], question["text"],
                                     answers=calendar_sync.numbered_answers(question["text"]) or None)
                send(chat_id, reply)
            else:
                edit(chat_id, int(question_id), f"{question['question']}\n\nNo, it's a note.")
                data = agent.load_sheet()
                reply, new_question = finish(writer, data, agent.start(client, data, question["text"]))
                send(chat_id, reply, question=new_question)
            return
        if question["kind"] == "note":
            label = {v: label for v, label, _ in agent.PERSON_CHOICES}.get(value)
            if label is None or not question.get("person"):
                return
            edit(chat_id, int(question_id), f"{question['text']}\n\nYour answer: {label}")
            show_typing(chat_id)
            reply, new_question = answer_question(client, writer, chat_id, question_id, question, label, value)
            send(chat_id, reply, question=new_question)
            return
        if question["kind"] != "calendar":
            return
        item, number = question["items"][0], question.get("number", 1)
        reply, remaining = calendar_sync.answer(client, writer, [item], "", answers={number: value}, numbers=[number])
        details = "\n\n".join(reply.split("\n\n")[1:])  # without "Answers recorded for question(s) n."
        close_calendar_question(chat_id, question_id, question,
                                f"Your answer: {calendar_sync.answer_label(item, value)}"
                                + (f"\n\n{details}" if details else ""))
        after_calendar_answers(client, writer, chat_id)
    except sheet_writer.WriteRefused as err:
        send(chat_id, f"Nothing was written: {err}")
    except Exception as err:
        traceback.print_exc()
        run_errors.append(f"button tap: {type(err).__name__}: {err}")
        send(chat_id, f"Something went wrong: {type(err).__name__}: {err}\n"
                      "Check the Changes tab for anything that was already written.")


def handle(client, writer, message):
    """Handle one message from the allowed chat and reply."""
    chat_id = message["chat"]["id"]
    text = message.get("text")
    if not text:
        send(chat_id, "I can only read text messages for now.")
        return

    reply_to = str(message.get("reply_to_message", {}).get("message_id", ""))
    question = load_questions().get(reply_to) if reply_to else None
    if question is None and text.strip().lower() == "/skip":
        send(chat_id, "To drop a question, reply /skip to that question's message.")
        return

    if question is None:
        group, open_questions = open_group(load_questions())
        if group and calendar_sync.numbered_answers(text):
            # Looks like answers to the open calendar questions: ask, don't guess.
            q_text = (f"This looks like answers to the {len(open_questions)} open calendar question(s). "
                      "Is it? (If not, I'll treat it as a note.)")
            send(chat_id, q_text, buttons=[("Yes, these are answers", "answers"), ("No, it's a note", "note")],
                 question={"kind": "confirm", "text": text, "group": group, "question": q_text})
            return

    print(f"\nNote: {text}" if question is None else f"\nAnswer to question {reply_to}: {text}")
    show_typing(chat_id)
    try:
        if question is None:
            data = agent.load_sheet()
            reply, new_question = finish(writer, data, agent.start(client, data, text))
        else:
            reply, new_question = answer_question(client, writer, chat_id, reply_to, question, text)
    except sheet_writer.WriteRefused as err:
        reply, new_question = f"Nothing was written: {err}\nSend it again if it still applies.", None
    except Exception as err:
        traceback.print_exc()
        run_errors.append(f"message: {type(err).__name__}: {err}")
        reply, new_question = (
            f"Something went wrong: {type(err).__name__}: {err}\n"
            "Check the Changes tab for anything that was already written."
        ), None
    send(chat_id, reply, question=new_question)


def run_calendar(client, writer, chat_id=None):
    """One calendar check: write what it decided, send its questions."""
    chat_id = chat_id or ALLOWED_CHAT_ID
    print("\nCalendar check...")
    try:
        plan = calendar_sync.check(client, open_keys=open_calendar_keys())
        summary = calendar_sync.apply(writer, plan)
        if summary:
            try:
                okr_edits, logged = okr.recount(writer)
                if okr_edits:
                    summary += "\n\n" + okr.describe(okr_edits) + "\n" + sheet_writer.logged_text(logged)
            except sheet_writer.WriteRefused as err:
                summary += f"\n\nOKR recount skipped: {err}"
            send(chat_id, summary)
        if plan["questions"]:
            send_calendar_questions(chat_id, plan["questions"])
        print(f"Calendar check done: {len(plan['adds'])} added, {len(plan['links'])} linked, "
              f"{len(plan['moves'])} moved, {len(plan['questions'])} question(s).")
    except Exception as err:
        traceback.print_exc()
        run_errors.append(f"calendar check: {type(err).__name__}: {err}")
        send(chat_id, f"The calendar check failed: {type(err).__name__}: {err}\nIt will run again in an hour.")


def recount_okrs(writer, chat_id=None):
    """The OKR recount at the end of a one-shot run (it also runs after every
    write); a change, such as a new month column, is reported on Telegram."""
    try:
        okr_edits, logged = okr.recount(writer)
        if okr_edits:
            send(chat_id or ALLOWED_CHAT_ID, okr.describe(okr_edits) + "\n" + sheet_writer.logged_text(logged))
    except sheet_writer.WriteRefused as err:
        print(f"OKR recount skipped: {err}")
    except Exception as err:
        traceback.print_exc()
        run_errors.append(f"OKR recount: {type(err).__name__}: {err}")


def handle_update(client, writer, update):
    """One Telegram update from getUpdates: a button tap or a message."""
    callback = update.get("callback_query")
    if callback:
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        if chat_id != ALLOWED_CHAT_ID or callback.get("from", {}).get("id") != ALLOWED_CHAT_ID:
            print(f"Ignored a button tap from chat {chat_id}.")
            return
        handle_tap(client, writer, callback)
        return

    # Edited messages are skipped: re-running the agent on an edit
    # would propose the same change twice.
    message = update.get("message")
    if not message:
        return
    chat_id = message.get("chat", {}).get("id")
    if chat_id != ALLOWED_CHAT_ID:
        print(f"Ignored a message from chat {chat_id}.")
        return
    handle(client, writer, message)


# A one-shot run fetches at most this many batches of updates (100 each).
ONCE_MAX_BATCHES = 20


def run_once(client, writer, calendar):
    """Handle every waiting update, run the calendar check and the OKR
    recount, then return. Returns the number of updates handled."""
    offset, handled = load_offset(), 0
    for _ in range(ONCE_MAX_BATCHES):
        # timeout=0: answer at once, also when nothing is waiting.
        response = retry.call("Telegram getUpdates", lambda: requests.get(
            f"{API}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=30).json())
        if not response.get("ok"):
            raise RuntimeError(f"Telegram getUpdates: {response.get('description')}")
        if not response["result"]:
            break
        for update in response["result"]:
            # Saved before handling, as in the polling loop.
            offset = update["update_id"] + 1
            save_offset(offset)
            handle_update(client, writer, update)
            handled += 1
    if calendar:
        run_calendar(client, writer)
    recount_okrs(writer)
    return handled


def main(reason_prefix="", calendar=None, once=False, force=False):
    """Poll Telegram and handle messages until Ctrl+C, or with once=True
    handle what is waiting and return (see run_once).

    test_bot.py runs this same function with Telegram faked and
    reason_prefix="test: ", so tests go through exactly this code.
    calendar overrides CALENDAR_SYNC (tests turn it off or on).
    force=True polls even if the Agent state tab says GitHub Actions runs the
    bot (two pollers would take each other's messages).
    """
    global calendar_active
    calendar = calendar_active = CALENDAR_SYNC if calendar is None else calendar
    if not once and not force and state.runner_in_sheet() == "github-actions":
        sys.exit("GitHub Actions runs this bot now (Agent state tab, key \"runner\").\n"
                 "Polling here as well would take its messages. Use --force only if Actions is switched off.")
    # Confirm the token works and show which bot we are connected to.
    # Tried until Telegram answers: a network error at start must not stop the bot.
    me = retry.call("Telegram getMe", lambda: requests.get(f"{API}/getMe", timeout=30).json(), attempts=10**9)
    if not me.get("ok"):
        sys.exit(f"Telegram rejected the token: {me.get('description')}")
    print(f"Connected as @{me['result']['username']}. Only answering chat {ALLOWED_CHAT_ID}.")
    print(f"Writing to the {agent.sheets.SHEET_TARGET} Sheet ({agent.sheets.SHEET_ID}).")
    print(f"Code version: {code_version()}")
    print(f"Calendar check: {'on' if calendar else 'off (set CALENDAR_SYNC=on to turn it on)'}")
    print(f"State: {'Agent state tab' if state.in_sheet() else 'local files'}")

    client = agent.make_client()
    writer = sheet_writer.Writer()
    writer.reason_prefix = reason_prefix
    migrate_batches(ALLOWED_CHAT_ID)
    if once:
        handled = run_once(client, writer, calendar)
        print(f"\nOne-shot run done: {handled} update(s) handled, {len(run_errors)} problem(s).")
        for problem in run_errors:
            print(f"  {problem}")
        return 1 if run_errors else 0

    print("Waiting for messages (Ctrl+C to stop).")
    offset = load_offset()
    next_calendar = 0  # run at start
    failures = 0  # getUpdates failures in a row

    while True:
        restart_if_code_changed()
        if calendar and agent.now().timestamp() >= next_calendar:
            next_calendar = agent.now().timestamp() + CALENDAR_EVERY
            run_calendar(client, writer)
        # timeout=30 tells Telegram to hold the connection open for up to
        # 30 seconds if there is nothing new, instead of replying immediately.
        try:
            response = requests.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=60,
            ).json()
            error = None if response.get("ok") else f"Error from Telegram: {response.get('description')}"
        except Exception as err:
            if not retry.is_network_error(err):
                raise
            error = f"Telegram getUpdates failed ({retry.describe(err)})"
        if error:
            failures += 1
            wait = retry.delay(failures)
            print(f"{error}, trying again in {wait} s")
            retry.sleep(wait)
            continue
        failures = 0

        for update in response["result"]:
            # Before the offset moves on, so after a restart this update is
            # fetched again and handled by the new code.
            restart_if_code_changed()
            # offset must be one higher than the last id we have seen.
            # Saved before handling, so a message that crashes the agent is
            # not retried forever.
            offset = update["update_id"] + 1
            save_offset(offset)
            handle_update(client, writer, update)


def option(args, name):
    """The value after `name` in the command line, or ""."""
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else ""


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        sys.exit(main(reason_prefix=option(args, "--reason-prefix"), once="--once" in args, force="--force" in args))
    except KeyboardInterrupt:
        print("\nStopped.")
