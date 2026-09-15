# Relationship Spice Dashboard

Personal relationship-suggestion dashboard: a Flask + SQLite backend serving a
single-file HTML/PWA frontend for browsing, filtering and tracking completion
of relationship "action" ideas across three categories (Actions, SMS,
Post-its).

## Layout

- `backend/server.py` — Flask API (`/api/actions`, toggle endpoints) + static file serving.
- `backend/setup_database.py` — populates `database.db` from source content.
- `backend/database.db` — SQLite database (actions table).
- `backend/*_cards.json` — source data for actions/SMS/post-it suggestions.
- `public/index.html` — frontend (Tailwind, vanilla JS, installable PWA).

## Run

```
cd backend
pip install -r requirements.txt
python server.py
```

Then open http://127.0.0.1:5000
