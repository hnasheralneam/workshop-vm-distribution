import json
import random
import threading
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

BASE_DIR = Path(__file__).parent
POOL_FILE = BASE_DIR / "pool.json"

app = Flask(__name__, static_folder=None)
lock = threading.Lock()


def load_pool():
    if not POOL_FILE.exists():
        return []
    with open(POOL_FILE) as f:
        return json.load(f)


pool = load_pool()


def save_pool():
    with open(POOL_FILE, "w") as f:
        json.dump(pool, f, indent=2)


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/status")
def status():
    rows = "".join(
        f"<tr><td>{entry['url']}</td><td>{'claimed' if entry['claimed'] else 'available'}</td></tr>"
        for entry in pool
    )
    return f"""<!DOCTYPE html>
<html>
<head><title>VM Status</title></head>
<body>
<h1>VM Status</h1>
<table border="1" cellpadding="4">
<tr><th>URL</th><th>Status</th></tr>
{rows}
</table>
</body>
</html>"""


@app.route("/api/claim")
def claim():
    with lock:
        available = [entry for entry in pool if not entry["claimed"]]
        if not available:
            return jsonify(detail="No VMs available right now. Please contact your instructor."), 404

        entry = random.choice(available)
        entry["claimed"] = True
        save_pool()
        return jsonify(url=entry["url"])


if __name__ == "__main__":
    print(f"Loaded {len(pool)} VM(s) from {POOL_FILE}")
    app.run(host="0.0.0.0", port=5000)
