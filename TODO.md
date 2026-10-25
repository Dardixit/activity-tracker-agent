# TODO

- [ ] Before deploying to GitHub Actions: switch Claude API auth from API key to identity federation.
- [ ] Switching to the real Sheet: share it with the service account as Editor, set `SHEET_TARGET=real`, run `setup_sheet.py` there once.
- [ ] Before GitHub Actions: give the local state files (`pending_questions.json`, `calendar_state.json`, `last_update_id.txt`) a durable home; they would be lost between runs.
- [ ] New Projects rows are appended without copying formatting from the row above.
- [ ] OKR month headers without a year ("Aug" to "Nov") are taken as 2026 (`okr.PLAIN_MONTH_YEAR`).
