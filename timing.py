"""Time each stage of one bot reply: Telegram round trips, Sheet reads, model calls.

Nothing is written to the Sheet and nothing is sent to the Telegram chat, but
each note is a real agent run, so it costs API tokens.

Run with:  .venv/bin/python timing.py                      (the CLAUDE_MODEL from .env)
       or: .venv/bin/python timing.py claude-opus-5-5 claude-haiku-5-5   (compare models)
"""

import contextlib
import io
import sys
import time

import anthropic
import requests

import bot
import agent
import sheets

NOTES = [
    "met Alex at the library today, follow up next week",
    "Northwind project kickoff on 23.10 went well",
]
MODELS = sys.argv[1:] or [agent.MODEL]

log = []


def timed(label, fn):
    def wrapper(*a, **k):
        t = time.perf_counter()
        r = fn(*a, **k)
        log.append((label, time.perf_counter() - t, r))
        return r
    return wrapper


# --- Telegram round trips (read-only calls; nothing is sent to the chat) ---
print("Telegram round trips")
for name, call in [
    ("getMe", lambda: requests.get(f"{bot.API}/getMe", timeout=30)),
    ("getUpdates(timeout=0)", lambda: requests.get(f"{bot.API}/getUpdates", params={"timeout": 0, "limit": 1}, timeout=30)),
]:
    times = []
    for _ in range(3):
        t = time.perf_counter(); call(); times.append(time.perf_counter() - t)
    print(f"  {name:<24} " + "  ".join(f"{x:.2f}s" for x in times))

# --- Sheet read, broken into its parts ---
sheets.load_credentials = timed("sheet: load key file", sheets.load_credentials)
agent.build = timed("sheet: build service", agent.build)
agent.read_tabs = timed("sheet: read tabs", agent.read_tabs)
agent.search_rows = timed("tool: search_rows", agent.search_rows)

client = anthropic.Anthropic()
client.messages.create = timed("model call", client.messages.create)

for model in MODELS:
    agent.MODEL = model
    for note in NOTES:
        log.clear()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            data = agent.load_sheet()
            run = agent.start(client, data, note)
            reply = agent.format_reply(data, run)
        total = time.perf_counter() - t0

        print(f"\n=== {model} | {note}")
        sums = {}
        for label, secs, r in log:
            sums[label] = sums.get(label, 0) + secs
            if label == "model call":
                u = r.usage
                tools = [b.name for b in r.content if b.type == "tool_use"] or ["(final text)"]
                print(f"  model call  {secs:6.2f}s  in={u.input_tokens:>6} out={u.output_tokens:>5}  -> {', '.join(tools)}")
        for label, secs in sums.items():
            print(f"  {label:<22} {secs:6.2f}s total")
        print(f"  {'END TO END':<22} {total:6.2f}s")
        print("  reply: " + reply.splitlines()[0] + " / " + " ".join(reply.split())[:160])
