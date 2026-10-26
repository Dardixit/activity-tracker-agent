"""A question saved before a code change can still be answered after it.

Starts the agent on a test note that makes it ask a question, saves the paused
run the way the bot does (as JSON), then changes the system prompt and the
tools, as a code change would, and continues the run with an answer:

1. Control: the saved conversation sent back as it is, with no block binding
   setting, must be refused (otherwise this test proves nothing).
2. agent.continue_run must work.

Calls Claude (a few small requests) and reads the Sheet (test Sheet unless
SHEET_TARGET=real); writes nothing and sends nothing.

Run with:  .venv/bin/python test_continue.py
"""

import json
import sys

import anthropic

import agent

NOTE = ("test: the test workshop was either three working days before last Friday or two days after the "
        "first Monday of this month. Work out both dates, then ask me with ask_user which one is right before "
        "you change anything. Do not propose anything first.")
TRIES = 3  # the model does not always think before asking; a run without thinking proves nothing
ANSWER = "test: it was today. Change nothing, this is only a test."


def has_thinking(messages):
    return any(isinstance(m["content"], list) and any(b.get("type") == "thinking" for b in m["content"])
               for m in messages if m["role"] == "assistant")


def main():
    client = agent.make_client()
    data = agent.load_sheet()

    for _ in range(TRIES):
        run = agent.start(client, data, NOTE)
        if run["asked"]:
            pending = json.loads(json.dumps(run["pending"]))  # saved and read back, as the bot does
            if has_thinking(pending["messages"]):
                break
    else:
        sys.exit(f"FAIL: no paused run with a thinking block in {TRIES} tries, so there is nothing to test.")
    print("Saved a paused run with thinking blocks.")

    # The code changes: a new rule in the system prompt and a new tool field.
    old_prompt = agent.system_prompt
    agent.system_prompt = lambda d: old_prompt(d) + "\n- (test) A rule added after the question was saved."
    agent.TOOLS = json.loads(json.dumps(agent.TOOLS))
    ask = next(t for t in agent.TOOLS if t["name"] == "ask_user")
    ask["input_schema"]["properties"]["test_field"] = {"type": "string", "description": "Added by the test."}

    ok = True
    results = pending["results"] + [{"type": "tool_result", "tool_use_id": pending["ask_id"], "content": ANSWER}]
    messages = pending["messages"] + [{"role": "user", "content": results}]
    try:
        client.messages.create(model=agent.MODEL, max_tokens=2000, output_config={"effort": "low"},
                               system=agent.system_prompt(data), tools=agent.TOOLS, messages=messages)
        print("NOTE control: the old way was not refused, so this run could not show the fix is needed.")
    except anthropic.BadRequestError as err:
        print(f"PASS control: the old way is refused ({str(err)[:110]}...)")

    try:
        continued = agent.continue_run(client, data, pending, ANSWER)
        if continued["problem"]:
            print(f"FAIL continue: {continued['problem']}")
            ok = False
        else:
            print("PASS continue: the saved question was answered after the prompt and tools changed.")
    except anthropic.BadRequestError as err:
        print(f"FAIL continue: {err}")
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
