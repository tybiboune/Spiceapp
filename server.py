
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.serving import make_server

# --- CONFIGURATION ---
# Everything (server.py, index.html, database.db, *.json) lives flat in one
# folder, mirroring the GitHub repo's layout 1:1 — this keeps the self-update
# logic below a trivial path-for-path sync instead of a directory mapping.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='/')
CORS(app) # Allow cross-origin requests

# --- SELF-UPDATE (pulls the latest files straight from GitHub) ---
GITHUB_OWNER = "tybiboune"
GITHUB_REPO = "Spiceapp"
GITHUB_BRANCH = "main"
# database.db holds the user's own done/progress state — an update must
# never overwrite it, however the repo's copy has changed.
UPDATE_EXCLUDED_PATHS = {"database.db"}
# Repo-root files that aren't part of the running app (docs, git config, …).
UPDATE_IGNORED_PATHS = {"README.md", ".gitignore"}
# Tracks the last commit SHA this install has synced to, purely local —
# never fetched from or written to GitHub — so /api/update/check can list
# the commits (and so the human-readable "what changed") since last time.
UPDATE_STATE_PATH = os.path.join(BASE_DIR, ".update_state.json")
# Git trailer lines that are implementation detail, not user-facing changelog.
COMMIT_TRAILER_PREFIXES = ("Co-Authored-By:", "Claude-Session:")
# Kept short (rather than a more generous 20-30s) so a flaky/unreachable
# connection (common on mobile) fails fast with a clear error instead of the
# "Checking for updates…" spinner sitting for tens of seconds per call. Only
# for the lightweight API calls below (tree/head/compare), whose responses
# are small JSON regardless of repo size.
GITHUB_TIMEOUT = 8
# Raw file downloads (_download_file) get their own, longer budget: some
# tracked files (the *_cards.json content files) are now well over 1MB, and
# an 8s cap that was fine for a few KB of source code isn't for those.
DOWNLOAD_TIMEOUT = 25
# Socket-level timeouts (what urlopen(timeout=...) actually gives you) only
# bound each individual blocking read, not the transfer as a whole — a
# connection trickling in a few bytes every few seconds never trips them and
# just hangs, which is exactly the "stuck downloading" symptom this guards
# against. Running the request in a worker thread and capping it with
# future.result(timeout=...) enforces a real wall-clock deadline instead.
_download_pool = ThreadPoolExecutor(max_workers=4)

def _with_deadline(fn, timeout):
    future = _download_pool.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(f"Timed out after {timeout}s")

def _repo_path_to_local(repo_path):
    """Repo layout is flat and mirrors BASE_DIR directly — no directory mapping needed."""
    if repo_path in UPDATE_IGNORED_PATHS or "/" in repo_path:
        return None
    return os.path.join(BASE_DIR, repo_path)

def _git_blob_sha1(data):
    """GitHub's tree API reports each file's *git blob* SHA, not a plain file
    hash — reproduce that so local files can be compared without downloading
    them first."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()

def _fetch_remote_tree():
    def _do():
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Spiceapp-updater"})
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            return json.loads(resp.read())
    data = _with_deadline(_do, GITHUB_TIMEOUT)
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]

def _find_updates():
    """Returns the list of repo-relative paths whose remote content differs
    from (or is missing from) the local copy."""
    changed = []
    for item in _fetch_remote_tree():
        repo_path = item["path"]
        if repo_path in UPDATE_EXCLUDED_PATHS:
            continue
        local_path = _repo_path_to_local(repo_path)
        if local_path is None:
            continue
        if os.path.isfile(local_path):
            with open(local_path, "rb") as f:
                if _git_blob_sha1(f.read()) == item["sha"]:
                    continue
        changed.append(repo_path)
    return changed

def _download_file_once(repo_path):
    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{repo_path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Spiceapp-updater"})
    # The per-call timeout here still helps (fails fast on a dead connection
    # that never sends a byte at all); _with_deadline below is what actually
    # bounds a *slow* one that keeps trickling data past DOWNLOAD_TIMEOUT.
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        return resp.read()

# Mobile connections drop a request mid-transfer often enough that failing
# the whole update batch on the first blip made "Update" feel unreliable
# even though a retry a moment later would usually just work. Content files
# are idempotent downloads (same URL, same bytes every time), so retrying
# is always safe.
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 1.5

def _download_file(repo_path):
    last_error = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            return _with_deadline(lambda: _download_file_once(repo_path), DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as e:
            # A 4xx (e.g. 404 — the path doesn't exist at this commit) is
            # never going to succeed on retry; only a 5xx from GitHub's side
            # is worth trying again.
            last_error = e
            if e.code < 500 or attempt == DOWNLOAD_RETRIES - 1:
                raise
            time.sleep(DOWNLOAD_RETRY_DELAY)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(DOWNLOAD_RETRY_DELAY)
    raise last_error

def _load_update_state():
    if os.path.isfile(UPDATE_STATE_PATH):
        try:
            with open(UPDATE_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def _save_update_state(state):
    with open(UPDATE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)

def _fetch_head_sha():
    def _do():
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Spiceapp-updater"})
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            return json.loads(resp.read())["sha"]
    return _with_deadline(_do, GITHUB_TIMEOUT)

def _fetch_commits_since(base_sha, head_sha):
    """Human-readable changelog: every commit message between the SHA this
    install last synced to and the current GitHub HEAD, subject/body split,
    with the git attribution trailers stripped (implementation detail, not
    a user-facing change). Returns [] on a first-ever sync (no base_sha
    stored yet) or if nothing changed — there's no meaningful "since" then."""
    if not base_sha or base_sha == head_sha:
        return []
    def _do():
        url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/compare/{base_sha}...{head_sha}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Spiceapp-updater"})
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            return json.loads(resp.read())
    data = _with_deadline(_do, GITHUB_TIMEOUT)
    commits = []
    for item in data.get("commits", []):
        message = item.get("commit", {}).get("message", "")
        lines = message.splitlines()
        subject = lines[0] if lines else ""
        body_lines = [
            line for line in lines[1:]
            if not line.strip().startswith(COMMIT_TRAILER_PREFIXES)
        ]
        body = "\n".join(body_lines).strip()
        if subject:
            commits.append({"subject": subject, "body": body})
    return commits

_httpd = None  # set once the server is actually listening, see __main__ below

def _restart_process():
    # Re-exec in place so the new server.py (if it changed) actually takes
    # effect — a plain return would keep running the code already in memory.
    #
    # Explicitly closing the listening socket first matters: retrying the
    # bind after exec (see the loop in __main__) only fixes a *transient*
    # "port still in use" race. If the socket's file descriptor survives
    # into the re-exec'd process instead of being closed by the OS (which
    # happened in practice — the same 'port in use' error kept recurring
    # across every retry, meaning nothing was ever going to free it), no
    # amount of retrying helps, because the new process is fighting a
    # socket it itself still holds open. Closing it here removes that
    # possibility outright rather than hoping the OS closes it for us.
    time.sleep(1)
    global _httpd
    if _httpd is not None:
        try:
            _httpd.server_close()
        except OSError:
            pass
    os.execv(sys.executable, [sys.executable] + sys.argv)

def get_db_connection():
    """Creates a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row # This allows accessing columns by name
    return conn

def _ensure_settings_table(conn):
    """Key/value store for small persistent app settings (cycle tracking
    toggle + last period date). Created lazily here too (not just in
    setup_database.py) so it also works against a database.db that predates
    this table, without forcing a full rebuild."""
    conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')

def _read_settings(conn):
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    values = {row['key']: row['value'] for row in rows}
    return {
        "cycleEnabled": values.get('cycle_enabled') == '1',
        "lastPeriodDate": values.get('last_period_date') or None,
    }

def _db_version():
    """Cheap fingerprint of the database file's current content (mtime +
    size), used as a cache key: the client keeps a local copy of the whole
    dataset and only needs to know when it's gone stale, not what changed."""
    try:
        st = os.stat(DB_PATH)
        return f"{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        return "0"

# --- API ROUTES ---

@app.route('/api/actions', methods=['GET'])
def get_actions():
    """Fetches all actions from the database and returns them as JSON."""
    try:
        conn = get_db_connection()
        actions = conn.execute('SELECT * FROM actions ORDER BY id').fetchall()
        conn.close()
        # Convert rows to a list of dictionaries
        response = jsonify([dict(ix) for ix in actions])
        response.headers['X-Actions-Version'] = _db_version()
        return response
    except sqlite3.OperationalError as e:
        # This likely means the database/table doesn't exist yet
        return jsonify({
            "error": "Database not initialized.",
            "message": "Please run the setup_database.py script first."
        }), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/actions/stream', methods=['GET'])
def stream_actions():
    """Streams every action as newline-delimited JSON (one row per line) so
    the client can start rendering cards as soon as the first rows arrive
    instead of waiting for the entire ~2000-row payload to download and
    parse. The client is expected to cache the result locally, keyed by the
    X-Actions-Version header, and only re-stream when that version changes."""
    client_version = request.headers.get('If-None-Match')
    version = _db_version()
    if client_version == version:
        return ('', 304, {'X-Actions-Version': version})

    def generate():
        conn = get_db_connection()
        try:
            cursor = conn.execute('SELECT * FROM actions ORDER BY id')
            for row in cursor:
                yield json.dumps(dict(row)) + "\n"
        finally:
            conn.close()

    response = app.response_class(generate(), mimetype='application/x-ndjson')
    response.headers['X-Actions-Version'] = version
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.route('/api/actions/<int:action_id>/toggle', methods=['POST'])
def toggle_done(action_id):
    """Toggles the isDone status for a given action."""
    try:
        conn = get_db_connection()
        # Invert the current boolean value
        conn.execute('UPDATE actions SET isDone = NOT isDone WHERE id = ?', (action_id,))
        conn.commit()
        
        # Return the updated action
        updated_action = conn.execute('SELECT * FROM actions WHERE id = ?', (action_id,)).fetchone()
        conn.close()
        
        if updated_action is None:
            return jsonify({"error": "Action not found"}), 404
            
        return jsonify(dict(updated_action))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/actions/<int:action_id>/variations/<int:variation_index>/toggle', methods=['POST'])
def toggle_variation_done(action_id, variation_index):
    """Toggles the isDone status for one of an action's example variations."""
    try:
        conn = get_db_connection()
        action = conn.execute('SELECT * FROM actions WHERE id = ?', (action_id,)).fetchone()
        if action is None:
            conn.close()
            return jsonify({"error": "Action not found"}), 404

        variations = json.loads(action['variations'] or '[]')
        if not (0 <= variation_index < len(variations)):
            conn.close()
            return jsonify({"error": "Variation not found"}), 404

        variations[variation_index]['isDone'] = not variations[variation_index].get('isDone', False)
        conn.execute('UPDATE actions SET variations = ? WHERE id = ?', (json.dumps(variations), action_id))
        conn.commit()

        updated_action = conn.execute('SELECT * FROM actions WHERE id = ?', (action_id,)).fetchone()
        conn.close()
        return jsonify(dict(updated_action))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/settings', methods=['GET'])
def get_settings():
    """Returns the cycle-tracking toggle and last period date, persisted
    server-side (database.db) so they survive closing the app, the same way
    done-progress does."""
    try:
        conn = get_db_connection()
        _ensure_settings_table(conn)
        settings = _read_settings(conn)
        conn.close()
        return jsonify(settings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/settings', methods=['POST'])
def update_settings():
    """Upserts whichever of cycleEnabled/lastPeriodDate is present in the
    request body, leaving the other untouched, and returns the full
    up-to-date settings."""
    body = request.get_json(silent=True) or {}
    try:
        conn = get_db_connection()
        _ensure_settings_table(conn)
        if 'cycleEnabled' in body:
            conn.execute(
                'INSERT INTO settings (key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                ('cycle_enabled', '1' if body['cycleEnabled'] else '0')
            )
        if 'lastPeriodDate' in body:
            conn.execute(
                'INSERT INTO settings (key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                ('last_period_date', body['lastPeriodDate'] or '')
            )
        conn.commit()
        settings = _read_settings(conn)
        conn.close()
        return jsonify(settings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/update/check', methods=['GET'])
def check_update():
    """Reports which files differ from the latest commit on GitHub, plus the
    human-readable commit messages since this install's last sync, without
    changing anything on disk."""
    try:
        # _find_updates() (tree fetch + local diffing) and _fetch_head_sha()
        # are independent GitHub calls — running them in parallel instead of
        # one after the other roughly halves the worst-case wait before
        # _fetch_commits_since() (which needs head_sha) can even start.
        with ThreadPoolExecutor(max_workers=2) as pool:
            changed_future = pool.submit(_find_updates)
            head_sha_future = pool.submit(_fetch_head_sha)
            changed = changed_future.result()
            head_sha = head_sha_future.result()
        state = _load_update_state()
        commits = _fetch_commits_since(state.get("lastSha"), head_sha)
        return jsonify({
            "updateAvailable": len(changed) > 0,
            "files": changed,
            "commits": commits,
            "headSha": head_sha,
        })
    except TimeoutError as e:
        return jsonify({"error": "Update check timed out — connection is too slow or was interrupted."}), 504
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return jsonify({"error": f"Could not reach GitHub: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/update/file', methods=['POST'])
def apply_update_file():
    """Downloads and overwrites exactly one file. The frontend calls this
    once per changed file (as reported by /api/update/check) so it can show
    real download progress instead of one opaque all-or-nothing request."""
    body = request.get_json(silent=True) or {}
    repo_path = body.get('path')
    if not repo_path:
        return jsonify({"error": "Missing 'path'."}), 400
    if repo_path in UPDATE_EXCLUDED_PATHS:
        return jsonify({"error": f"'{repo_path}' is never overwritten by an update."}), 400
    local_path = _repo_path_to_local(repo_path)
    if local_path is None:
        return jsonify({"error": f"'{repo_path}' is not an updatable app file."}), 400
    try:
        content = _download_file(repo_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(content)
        return jsonify({"path": repo_path})
    except TimeoutError as e:
        return jsonify({"error": f"'{repo_path}' download timed out after {DOWNLOAD_RETRIES} attempt(s) — connection is too slow or was interrupted."}), 504
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return jsonify({"error": f"Could not reach GitHub: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/update/mark-applied', methods=['POST'])
def mark_update_applied():
    """Records the GitHub commit SHA this install has now synced to, so the
    *next* check's changelog starts from here instead of replaying commits
    already applied. Called by the frontend once every changed file has been
    downloaded successfully."""
    body = request.get_json(silent=True) or {}
    head_sha = body.get('headSha')
    if not head_sha:
        return jsonify({"error": "Missing 'headSha'."}), 400
    _save_update_state({"lastSha": head_sha})
    return jsonify({"ok": True})

@app.route('/api/update/restart', methods=['POST'])
def restart_after_update():
    """Triggers the self-restart once the frontend has applied every file
    from an update batch that included at least one changed .py file."""
    threading.Thread(target=_restart_process, daemon=True).start()
    return jsonify({"restarting": True})

# --- STATIC FILE SERVING ---

@app.route('/')
def serve_index():
    """Serves the main index.html file."""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    """Serves other static files (not strictly needed with default config but good practice)."""
    return send_from_directory(app.static_folder, path)

# --- AUTOMATIC DATABASE BACKUP ---
# database.db (the user's actual progress) lives only inside this install's
# own folder. On Termux specifically that folder has turned out to not be as
# safe as assumed: an Android "clear cache" action on the Termux app can, on
# some versions/OEMs, wipe the whole $HOME instead of just a cache partition
# — which is exactly what happened once already. Copying the database out to
# the phone's shared storage (outside Termux's own private storage) means a
# repeat of that no longer erases progress a second time. This is entirely
# best-effort: if shared storage isn't set up (`termux-setup-storage` never
# run) or this isn't Termux at all, it just silently does nothing rather than
# ever affecting the running app.
BACKUP_DIR = os.path.expanduser("~/storage/shared/SpiceappBackups")
BACKUP_INTERVAL_SECONDS = 60 * 60  # also re-backed-up hourly for long-running sessions, not just on launch
BACKUP_KEEP_COUNT = 10  # rotate old backups so this doesn't grow forever

def _backup_database_once():
    if not os.path.isfile(DB_PATH):
        return
    shared_storage_root = os.path.dirname(BACKUP_DIR)
    if not os.path.isdir(shared_storage_root):
        return  # shared storage not available/mounted — nothing to do
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"database_{timestamp}.db")
        shutil.copy2(DB_PATH, dest)
        backups = sorted(
            f for f in os.listdir(BACKUP_DIR)
            if f.startswith("database_") and f.endswith(".db")
        )
        for stale in backups[:-BACKUP_KEEP_COUNT]:
            try:
                os.remove(os.path.join(BACKUP_DIR, stale))
            except OSError:
                pass
        print(f"   - Database backed up to {dest}")
    except OSError as e:
        print(f"   - Database backup skipped (couldn't write to shared storage: {e})")

def _backup_loop():
    while True:
        _backup_database_once()
        time.sleep(BACKUP_INTERVAL_SECONDS)

# --- MAIN EXECUTION ---

if __name__ == '__main__':
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    print("--- Starting Flask Server ---")
    print("Your app will be available at: http://127.0.0.1:5000")
    print("-----------------------------")
    # Built directly with werkzeug.serving.make_server() instead of
    # app.run(): that's the only way to keep a handle on the actual listening
    # socket (_httpd), which _restart_process() needs to explicitly close
    # before re-exec'ing — see the comment there for why that matters. This
    # also sidesteps app.run()'s reloader machinery entirely (no
    # use_reloader flag needed), which used to race with our own restart the
    # moment an update wrote a new server.py.
    if debug_mode:
        from werkzeug.debug import DebuggedApplication
        wsgi_app = DebuggedApplication(app, evalex=True)
    else:
        wsgi_app = app

    # Retry on EADDRINUSE: even with the socket explicitly closed before
    # exec (see _restart_process), the OS can still take a brief moment to
    # actually free the port — this rides out that window instead of dying
    # on it.
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        try:
            _httpd = make_server('0.0.0.0', 5000, wsgi_app, threaded=True)
            break
        except OSError as e:
            if e.errno not in (98, 48) or attempt == max_attempts:  # EADDRINUSE: 98 Linux/Android, 48 macOS
                raise
            print(f"   - Port 5000 still in use (attempt {attempt}/{max_attempts}), retrying in 1s…")
            time.sleep(1)

    threading.Thread(target=_backup_loop, daemon=True).start()
    _httpd.serve_forever()
