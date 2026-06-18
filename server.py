import json
import random
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).parent
POOL_FILE = BASE_DIR / "pool.json"
POOL_POLL_INTERVAL_SECONDS = 5

app = Flask(__name__, static_folder=None)
lock = threading.Lock()


def load_pool():
    if not POOL_FILE.exists():
        return []
    with open(POOL_FILE) as f:
        return json.load(f)


pool = load_pool()
pool_mtime = POOL_FILE.stat().st_mtime if POOL_FILE.exists() else None


def save_pool():
    global pool_mtime
    with open(POOL_FILE, "w") as f:
        json.dump(pool, f, indent=2)
    pool_mtime = POOL_FILE.stat().st_mtime


def watch_pool_file():
    """Background thread: picks up VMs added/removed on disk by the provisioner/destroyer."""
    global pool, pool_mtime
    while True:
        time.sleep(POOL_POLL_INTERVAL_SECONDS)
        try:
            mtime = POOL_FILE.stat().st_mtime if POOL_FILE.exists() else None
        except OSError:
            continue

        if mtime != pool_mtime:
            with lock:
                pool = load_pool()
                pool_mtime = mtime
            print(f"Reloaded pool.json: now {len(pool)} VM(s)")


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


@app.route("/api/validate")
def validate():
    url = request.args.get("url", "")
    with lock:
        still_claimed = any(entry["url"] == url and entry["claimed"] for entry in pool)
    return jsonify(valid=still_claimed)


if __name__ == "__main__":
    print(f"Loaded {len(pool)} VM(s) from {POOL_FILE}")
    threading.Thread(target=watch_pool_file, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
