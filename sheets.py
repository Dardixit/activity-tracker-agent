"""Read rows from a Google Sheet using a service account.

Run with:  .venv/bin/python sheets.py
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import retry

load_dotenv()

# GOOGLE_SHEET_ID is the real Sheet. Until SHEET_TARGET=real is set on
# purpose, everything works on the test copy in GOOGLE_TEST_SHEET_ID.
REAL_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
TEST_SHEET_ID = os.getenv("GOOGLE_TEST_SHEET_ID", "").strip()
SHEET_TARGET = os.getenv("SHEET_TARGET", "test").strip().lower()
SHEET_ID = REAL_SHEET_ID if SHEET_TARGET == "real" else TEST_SHEET_ID
HERE = Path(__file__).parent

# Tab names and the Activity Log categories (see .env.example).
ACTIVITY_TAB = os.getenv("ACTIVITY_TAB", "").strip() or "Activity Log"
PROJECTS_TAB = os.getenv("PROJECTS_TAB", "").strip() or "Projects"
DEFAULT_CATEGORIES = "Networking,Outreach,Project,Meeting,Learning,Personal Brand,Preparation"
CATEGORIES = [c.strip() for c in (os.getenv("CATEGORIES", "").strip() or DEFAULT_CATEGORIES).split(",") if c.strip()]
TAB_NAME = ACTIVITY_TAB

# "readonly" means Google itself refuses any write from these credentials,
# even if the code asked for one.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
# Read and write. Only sheet_writer.py asks for this.
WRITE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def find_key_file():
    """Locate the service account JSON key in this folder.

    Prefers service-account.json, otherwise picks any .json file that is
    actually a service account key (so Google's random download name works).
    """
    preferred = HERE / "service-account.json"
    if preferred.exists():
        return preferred

    for path in sorted(HERE.glob("*.json")):
        try:
            if json.loads(path.read_text()).get("type") == "service_account":
                return path
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return None


def load_credentials(write=False):
    key_path = find_key_file()
    if key_path is None:
        sys.exit(
            "No service account key found in this folder.\n"
            "Download the JSON key from Google Cloud Console and save it here as:\n"
            f"  {HERE / 'service-account.json'}"
        )

    info = json.loads(key_path.read_text())
    email = info.get("client_email", "<no client_email in key>")
    print(f"Using key:   {key_path.name}")
    print(f"Robot email: {email}")
    if write:
        print(f"  -> The Sheet must be shared with this address as Editor.\n")
    else:
        print(f"  -> The Sheet must be shared with this address (Viewer is enough).\n")

    creds = service_account.Credentials.from_service_account_file(
        str(key_path), scopes=WRITE_SCOPES if write else SCOPES
    )
    return creds, email


def list_tabs(service):
    meta = retry.execute(
        service.spreadsheets().get(spreadsheetId=SHEET_ID, fields="sheets.properties.title"), "Sheets read")
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def read_rows(service, tab):
    # Quotes around the tab name are required because "Activity Log" has a space.
    result = retry.execute(
        service.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=f"'{tab}'"), "Sheets read")
    return result.get("values", [])


def explain_error(err, email):
    """Translate Google's API errors into something actionable."""
    text = str(err)
    if "SERVICE_DISABLED" in text or "has not been used in project" in text:
        return (
            "The Google Sheets API is not enabled for this project yet.\n"
            "In Google Cloud Console: APIs & Services > Library > "
            "search 'Google Sheets API' > Enable."
        )
    if "PERMISSION_DENIED" in text or "caller does not have permission" in text:
        return (
            "The Sheet has not been shared with the robot account.\n"
            f"Open the Sheet, click Share, and add:\n  {email}\n"
            "Give it Viewer access, then run this again."
        )
    if "NOT_FOUND" in text or "Requested entity was not found" in text:
        return (
            f"No Sheet found with ID {SHEET_ID!r}.\n"
            "Check GOOGLE_SHEET_ID / GOOGLE_TEST_SHEET_ID in your .env file."
        )
    return None


def main():
    if not SHEET_ID:
        name = "GOOGLE_SHEET_ID" if SHEET_TARGET == "real" else "GOOGLE_TEST_SHEET_ID"
        sys.exit(f"{name} is missing from your .env file (SHEET_TARGET={SHEET_TARGET}).")

    creds, email = load_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    try:
        tabs = list_tabs(service)
    except HttpError as err:
        hint = explain_error(err, email)
        sys.exit(hint if hint else f"Google returned an error:\n{err}")

    if TAB_NAME not in tabs:
        sys.exit(
            f"No tab named {TAB_NAME!r} in this Sheet.\n"
            f"Tabs that do exist: {', '.join(repr(t) for t in tabs)}\n"
            "Fix the name in .env (ACTIVITY_TAB) or rename the tab."
        )

    rows = read_rows(service, TAB_NAME)
    if not rows:
        print(f"Tab {TAB_NAME!r} is empty.")
        return

    header, data = rows[0], rows[1:]
    last_five = data[-5:]

    print(f"Tab {TAB_NAME!r}: {len(data)} data rows (plus a header row).")
    print(f"Columns: {' | '.join(header)}")
    print(f"\nLast {len(last_five)} row(s):")
    for row in last_five:
        print("  " + " | ".join(row))


if __name__ == "__main__":
    main()
