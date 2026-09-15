
import json
import os
import sqlite3
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# --- CONFIGURATION ---
# Anchor paths to this file's location so the server works regardless of the
# directory it's launched from (previously a relative "backend/database.db"
# combined with `cd backend` in launch_app.bat silently created a nested
# backend/backend/ folder).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
# Normal layout: backend/server.py + public/index.html as siblings.
# Also tolerate a flat single-folder drop (server.py, database.db, index.html
# all in the same directory, e.g. a simplified Termux copy) by falling back
# to BASE_DIR itself when there's no sibling "public" folder.
_sibling_public = os.path.join(BASE_DIR, "..", "public")
PUBLIC_DIR = _sibling_public if os.path.isdir(_sibling_public) else BASE_DIR

app = Flask(__name__, static_folder=PUBLIC_DIR, static_url_path='/')
CORS(app) # Allow cross-origin requests

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
    app.run(debug=debug_mode, port=5000)
