import json
import random
import threading
import time
from datetime import datetime
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


def is_expired(entry):
    return entry.get("expires_at") is not None and time.time() >= entry["expires_at"]


def is_available(entry):
    return not entry["claimed"] and not is_expired(entry)


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
    def row(entry):
        status_label = "expired" if is_expired(entry) else ("claimed" if entry["claimed"] else "available")
        expires_at = entry.get("expires_at")
        expires_label = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S") if expires_at else "n/a"
        return f"<tr><td>{entry['url']}</td><td>{status_label}</td><td>{expires_label}</td></tr>"

    rows = "".join(row(entry) for entry in pool)
    return f"""<!DOCTYPE html>
<html>
<head><title>VM Status</title></head>
<body>
<h1>VM Status</h1>
<table border="1" cellpadding="4">
<tr><th>URL</th><th>Status</th><th>Expires</th></tr>
{rows}
</table>
</body>
</html>"""


@app.route("/api/claim")
def claim():
    with lock:
        available = [entry for entry in pool if is_available(entry)]
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
        entry = next((e for e in pool if e["url"] == url and e["claimed"]), None)
        if entry is None:
            return jsonify(valid=False)

        if is_expired(entry):
            entry["claimed"] = False

            available = [e for e in pool if is_available(e)]
            if not available:
                save_pool()
                return jsonify(valid=False, expired=True)

            replacement = random.choice(available)
            replacement["claimed"] = True
            save_pool()
            return jsonify(valid=False, expired=True, url=replacement["url"])

        return jsonify(valid=True)


if __name__ == "__main__":
    print(f"Loaded {len(pool)} VM(s) from {POOL_FILE}")
    threading.Thread(target=watch_pool_file, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
