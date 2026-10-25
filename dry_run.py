"""Dry run of the agent: Telegram note -> proposed Sheet change, printed only.

Nothing is written to the Sheet: this uses read-only Google credentials and
never calls sheet_writer.py. A question from the agent is printed and the run stops.

Run with:  .venv/bin/python dry_run.py "met Alex at the library today, follow up next week"
       or: .venv/bin/python dry_run.py          (runs the three built-in test messages)
"""

import sys

import agent

TEST_MESSAGES = [
    "met Alex at the library today, follow up next week",
    "Northwind project kickoff on 23.10 went well",
    "registered for the Example Tech Summit side event",
]


def main():
    notes = sys.argv[1:] or TEST_MESSAGES
    data = agent.load_sheet()
    client = agent.make_client()

    for note in notes:
        print("=" * 78)
        print(f"Note: {note}")
        print("=" * 78)
        run = agent.start(client, data, note)
        print("\n  --- Reply (not written to the Sheet) ---")
        print("  " + agent.format_reply(data, run).replace("\n", "\n  "))
        print()


if __name__ == "__main__":
    main()
