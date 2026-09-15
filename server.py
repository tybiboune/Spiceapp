
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

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
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Spiceapp-updater"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
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

def _download_file(repo_path):
    url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{repo_path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Spiceapp-updater"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()

def _restart_process():
    # Re-exec in place so the new server.py (if it changed) actually takes
    # effect — a plain return would keep running the code already in memory.
    time.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

def get_db_connection():
    """Creates a connection to the SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row # This allows accessing columns by name
    return conn

# --- API ROUTES ---

@app.route('/api/actions', methods=['GET'])
def get_actions():
    """Fetches all actions from the database and returns them as JSON."""
    try:
        conn = get_db_connection()
        actions = conn.execute('SELECT * FROM actions ORDER BY id').fetchall()
        conn.close()
        # Convert rows to a list of dictionaries
        return jsonify([dict(ix) for ix in actions])
    except sqlite3.OperationalError as e:
        # This likely means the database/table doesn't exist yet
        return jsonify({
            "error": "Database not initialized.",
            "message": "Please run the setup_database.py script first."
        }), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

@app.route('/api/update/check', methods=['GET'])
def check_update():
    """Reports which files differ from the latest commit on GitHub, without changing anything."""
    try:
        changed = _find_updates()
        return jsonify({"updateAvailable": len(changed) > 0, "files": changed})
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
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return jsonify({"error": f"Could not reach GitHub: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

# --- MAIN EXECUTION ---

if __name__ == '__main__':
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    print("--- Starting Flask Server ---")
    print("Your app will be available at: http://127.0.0.1:5000")
    print("-----------------------------")
    # use_reloader=False: Werkzeug's own file-watching auto-restart would
    # otherwise race with our explicit self-update restart (_restart_process)
    # the moment an update writes a new server.py — the two would fight over
    # restarting the same process, sometimes tearing down the connection
    # before the /api/update/* response reaches the browser.
    app.run(debug=debug_mode, port=5000, threaded=True, use_reloader=False)
