import json
import os
import random
import threading
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

import destroy
import provision

load_dotenv()

BASE_DIR = Path(__file__).parent
POOL_FILE = BASE_DIR / "pool.json"
POOL_POLL_INTERVAL_SECONDS = 5
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

app = Flask(__name__, static_folder=None)
lock = threading.Lock()

job_lock = threading.Lock()
current_job = None  # {"id", "kind", "status", "log": [...], "started_at", "finished_at"}


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


def os_type(entry):
    """rdp is always Windows; ssh/vnc (and legacy entries with no access_method) are Linux."""
    return "windows" if entry.get("access_method") == "rdp" else "linux"


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
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.route("/style.css")
def style():
    return send_from_directory(BASE_DIR / "static", "style.css")


@app.route("/api/types")
def types():
    with lock:
        available_types = sorted({os_type(entry) for entry in pool if is_available(entry)})
        return jsonify(types=available_types)


@app.route("/api/claim")
def claim():
    requested_os = request.args.get("os")
    with lock:
        available = [entry for entry in pool if is_available(entry)]
        if requested_os:
            available = [entry for entry in available if os_type(entry) == requested_os]
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

            available = [e for e in pool if is_available(e) and os_type(e) == os_type(entry)]
            if not available:
                save_pool()
                return jsonify(valid=False, expired=True)

            replacement = random.choice(available)
            replacement["claimed"] = True
            save_pool()
            return jsonify(valid=False, expired=True, url=replacement["url"])

        return jsonify(valid=True)


def reload_pool():
    global pool, pool_mtime
    with lock:
        pool = load_pool()
        pool_mtime = POOL_FILE.stat().st_mtime if POOL_FILE.exists() else None


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return jsonify(detail="ADMIN_PASSWORD is not set on the server."), 503
        auth = request.authorization
        if not auth or auth.password != ADMIN_PASSWORD:
            return jsonify(detail="Authentication required."), 401, {
                "WWW-Authenticate": 'Basic realm="Admin"'
            }
        return f(*args, **kwargs)
    return wrapper


def job_log_appender(job):
    def append(message):
        print(message)
        job["log"].append(str(message))
    return append


def start_job(kind, target, *target_args):
    """Runs target(*target_args, log=...) in a background thread. Only one job at a time."""
    global current_job
    with job_lock:
        if current_job is not None and current_job["status"] == "running":
            return None
        job = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "status": "running",
            "log": [],
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
        }
        current_job = job

    def runner():
        try:
            target(*target_args, log=job_log_appender(job))
            job["status"] = "done"
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["log"].append(f"❌ Job failed: {exc}")
        finally:
            job["finished_at"] = time.time()
            reload_pool()

    threading.Thread(target=runner, daemon=True).start()
    return job


@app.route("/admin")
@admin_required
def admin_page():
    return send_from_directory(BASE_DIR / "static", "admin.html")


@app.route("/api/admin/defaults")
@admin_required
def admin_defaults():
    config = provision.build_config()
    safe = {k: v for k, v in config.items() if k not in provision.SECRET_FIELDS}
    return jsonify(safe)


@app.route("/api/admin/pool")
@admin_required
def admin_pool():
    with lock:
        entries = []
        for entry in pool:
            entries.append({
                "vmid": entry.get("vmid"),
                "student_id": entry.get("student_id"),
                "url": entry.get("url"),
                "claimed": entry.get("claimed"),
                "expired": is_expired(entry),
                "expires_at": entry.get("expires_at"),
            })
        return jsonify(entries)


@app.route("/api/admin/provision", methods=["POST"])
@admin_required
def admin_provision():
    body = request.get_json(silent=True) or {}
    overrides = {k: v for k, v in body.items() if k != "vm_count"}
    try:
        config = provision.build_config(overrides)
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    count = body.get("vm_count")
    try:
        count = int(count) if count not in (None, "") else None
    except (ValueError, TypeError):
        return jsonify(detail="vm_count must be an integer."), 400

    job = start_job("provision", provision.run_parallel_provisioning, config, count)
    if job is None:
        return jsonify(detail="A job is already running."), 409
    return jsonify(job_id=job["id"])


@app.route("/api/admin/destroy", methods=["POST"])
@admin_required
def admin_destroy():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode not in ("all", "expired", "specific"):
        return jsonify(detail="mode must be 'all', 'expired', or 'specific'."), 400

    vmids = body.get("vmids") or []
    if mode == "specific" and not vmids:
        return jsonify(detail="vmids is required when mode is 'specific'."), 400

    config = destroy.build_config()
    job = start_job("destroy", destroy.run_teardown, config, mode, vmids)
    if job is None:
        return jsonify(detail="A job is already running."), 409
    return jsonify(job_id=job["id"])


@app.route("/api/admin/job")
@app.route("/api/admin/job/<job_id>")
@admin_required
def admin_job(job_id=None):
    if current_job is None or (job_id and current_job["id"] != job_id):
        return jsonify(detail="No such job."), 404
    return jsonify(current_job)


if __name__ == "__main__":
    print(f"Loaded {len(pool)} VM(s) from {POOL_FILE}")
    threading.Thread(target=watch_pool_file, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
