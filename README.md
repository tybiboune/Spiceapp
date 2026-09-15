# Relationship Spice Dashboard

Personal relationship-suggestion dashboard: a Flask + SQLite backend serving a
single-file HTML/PWA frontend for browsing, filtering and tracking completion
of relationship "action" ideas across three categories (Actions, SMS,
Post-its).

## Layout

Everything lives flat in the repo root — this mirrors the local install
exactly, which is what the in-app **Update** button relies on to sync files
file-for-file from GitHub.

- `server.py` — Flask API (`/api/actions`, toggle endpoints, self-update) + static file serving.
- `setup_database.py` — populates `database.db` from source content.
- `database.db` — SQLite database (actions table). Never touched by Update — it holds your progress.
- `*_cards.json` — source data for actions/SMS/post-it suggestions.
- `index.html` — frontend (Tailwind, vanilla JS, installable PWA).

## Run

```
pip install -r requirements.txt
python server.py
```

Then open http://127.0.0.1:5000

## Updating

Click **Update** in the app. It checks this repo for changed files, downloads
them, and restarts the server automatically if any `.py` file changed. The
repo must be public for this to work (no auth token is configured).
