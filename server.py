import fcntl
import hmac
import json
import os
import random
import secrets
import threading
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import destroy
import provision

load_dotenv()

BASE_DIR = Path(__file__).parent
POOL_FILE = BASE_DIR / "pool.json"
CONFIGS_FILE = BASE_DIR / "configs.json"
POOL_POLL_INTERVAL_SECONDS = 5
REAP_INTERVAL_SECONDS = int(os.getenv("REAP_INTERVAL_SECONDS", "60"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])
lock = threading.Lock()

job_lock = threading.Lock()
current_job = None  # {"id", "kind", "status", "log": [...], "started_at", "finished_at"}


def load_pool():
    if not POOL_FILE.exists():
        return []
    with open(POOL_FILE, "r") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            data = json.load(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return data


pool = load_pool()
pool_mtime = POOL_FILE.stat().st_mtime if POOL_FILE.exists() else None


def save_pool():
    global pool_mtime
    tmp = POOL_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(pool, f, indent=2)
            f.flush()
            os.fchmod(f.fileno(), 0o600)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    os.replace(str(tmp), str(POOL_FILE))
    pool_mtime = POOL_FILE.stat().st_mtime


def is_expired(entry):
    return entry.get("expires_at") is not None and time.time() >= entry["expires_at"]


def is_available(entry):
    return not entry["claimed"] and not is_expired(entry)


def os_type(entry):
    """rdp is always Windows; ssh/vnc (and legacy entries with no access_method) are Linux."""
    return "windows" if entry.get("access_method") == "rdp" else "linux"


def display_name(entry):
    return entry.get("pool") or ("Windows" if entry.get("access_method") == "rdp" else "Linux")


def load_configs():
    if not CONFIGS_FILE.exists():
        return {}
    with open(CONFIGS_FILE, "r") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def save_configs(configs):
    tmp = CONFIGS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(configs, f, indent=2)
            f.flush()
            os.fchmod(f.fileno(), 0o600)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    os.replace(str(tmp), str(CONFIGS_FILE))


def config_pool_name(config):
    return config.get("pool_name") or ("Windows" if config.get("template_vm_access_method") == "rdp" else "Linux")


CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_pool_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))


def gated_pools():
    return {name for name, cfg in load_configs().items() if cfg.get("pool_code")}


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


def reap_expired_vms():
    """Background thread: destroys VMs on Proxmox once their expires_at time limit passes.

    Delegates to destroy.run_teardown(mode="expired"), which stops/destroys the VMs
    and prunes them from pool.json; watch_pool_file then reloads the pruned pool.
    """
    while True:
        time.sleep(REAP_INTERVAL_SECONDS)
        try:
            entries = load_pool()
        except (OSError, json.JSONDecodeError):
            continue
        if not any(is_expired(entry) for entry in entries):
            continue
        with job_lock:
            job_running = current_job is not None and current_job["status"] == "running"
        if job_running:
            print("Reaper: skipping cycle, an admin job is running")
            continue
        try:
            results = destroy.run_teardown(destroy.build_config(), mode="expired")
            print(f"Reaper: destroyed {len(results)} expired VM(s)")
        except Exception as exc:
            print(f"Reaper: teardown failed, will retry next cycle: {exc}")


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.route("/claim/<path:label>")
def claim_link(label):
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.route("/style.css")
def style():
    return send_from_directory(BASE_DIR / "static", "style.css")


@app.route("/images/<path:filename>")
def images(filename):
    return send_from_directory(BASE_DIR / "images", filename)


@app.route("/api/types")
def types():
    gated = gated_pools()
    with lock:
        pools = {}
        for entry in pool:
            if not is_available(entry) or display_name(entry) in gated:
                continue
            name = display_name(entry)
            group = pools.setdefault(name, {"name": name, "os": os_type(entry), "available": 0})
            group["available"] += 1
        return jsonify(pools=sorted(pools.values(), key=lambda p: p["name"]), coded=len(gated))


@app.route("/api/claim", methods=["POST"])
@limiter.limit("10/minute")
def claim():
    body = request.get_json(silent=True) or {}
    requested_pool = body.get("pool")
    requested_os = body.get("os")
    gated = gated_pools()
    with lock:
        available = [entry for entry in pool if is_available(entry)]
        if requested_pool:
            available = [entry for entry in available if display_name(entry) == requested_pool]
        else:
            available = [entry for entry in available if display_name(entry) not in gated]
            if requested_os:
                available = [entry for entry in available if os_type(entry) == requested_os]
        if not available:
            return jsonify(detail="No VMs available right now. Please contact your instructor."), 404

        entry = random.choice(available)
        entry["claimed"] = True
        try:
            entry["url"] = provision.mint_session_url(entry)
        except Exception as exc:
            entry["claimed"] = False
            save_pool()
            return jsonify(detail=f"VM is not reachable right now ({exc}). Please contact your instructor."), 502
        save_pool()
        return jsonify(url=entry["url"])


@app.route("/api/validate", methods=["POST"])
@limiter.limit("30/minute")
def validate():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    with lock:
        entry = next((e for e in pool if e["url"] == url and e["claimed"]), None)
        if entry is None:
            return jsonify(valid=False)

        if is_expired(entry):
            entry["claimed"] = False
            available = [e for e in pool if is_available(e) and display_name(e) == display_name(entry)]
            if not available:
                available = [e for e in pool if is_available(e) and os_type(e) == os_type(entry)
                             and display_name(e) not in gated_pools()]
            if not available:
                save_pool()
                return jsonify(valid=False, expired=True, expires_at=entry.get("expires_at"))

            replacement = random.choice(available)
            try:
                replacement["url"] = provision.mint_session_url(replacement)
            except Exception as exc:
                save_pool()
                return jsonify(valid=False, expired=True, detail=str(exc))
            replacement["claimed"] = True
            save_pool()
            return jsonify(valid=False, expired=True, url=replacement["url"])

        return jsonify(valid=True)


@app.route("/api/reconnect", methods=["POST"])
@limiter.limit("10/minute")
def reconnect():
    """Mint a FRESH Guacamole session for a previously assigned workshop VM.

    Stored ?token= URLs are Guacamole session tokens (~60min idle timeout),
    so replaying them later lands on the login page. Re-minting at click time
    (with the VM's current IP) keeps Reconnect working for the pool lifetime.
    """
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    with lock:
        entry = next((e for e in pool if e.get("url") == url), None)
        if entry is None:
            return jsonify(valid=False), 404
        if is_expired(entry):
            return jsonify(valid=False, expired=True), 410
        try:
            fresh = provision.mint_session_url(entry)
        except Exception as exc:
            return jsonify(valid=False, detail=str(exc)), 502
        entry["url"] = fresh
        save_pool()
        return jsonify(valid=True, url=fresh)


@app.route("/api/release", methods=["POST"])
@limiter.limit("10/minute")
def release():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    with lock:
        entry = next((e for e in pool if e.get("url") == url and e.get("claimed")), None)
        if entry is None:
            return jsonify(detail="No assigned machine found."), 404
        pool.remove(entry)
        save_pool()

    def destroy_released():
        try:
            config = destroy.build_config()
            proxmox = destroy.get_proxmox_client(config)
            destroy.destroy_worker(proxmox, config["proxmox_node"], entry["vmid"], f"workshop-{entry.get('student_id')}", print)
        except Exception as exc:
            entry["expires_at"] = time.time()
            with lock:
                pool.append(entry)
                save_pool()
            print(f"Release: destroying VM {entry['vmid']} failed, marked expired for reaper retry: {exc}")

    threading.Thread(target=destroy_released, daemon=True).start()
    return jsonify(released=True)


redeem_tickets = {}


def find_pool_by_code(code):
    code = (code or "").strip().upper()
    if not code:
        return None
    for config in load_configs().values():
        stored = (config.get("pool_code") or "").strip().upper()
        if stored and hmac.compare_digest(stored, code):
            return config
    return None


def set_ticket(ticket, **fields):
    with lock:
        if ticket in redeem_tickets:
            redeem_tickets[ticket].update(fields)


def run_redeem_provision(ticket, config):
    def log(message):
        print(message)
        text = str(message).lower()
        if "booting" in text:
            set_ticket(ticket, stage="booting")
        elif "waiting for ip" in text:
            set_ticket(ticket, stage="network")

    try:
        vmid, student_id, url, expires_at = provision.provision_one(
            config, f"student-{uuid.uuid4().hex[:6]}", log)
        set_ticket(ticket, stage="adding")
        entry = {
            "vmid": vmid,
            "student_id": student_id,
            "url": url,
            "claimed": True,
            "expires_at": expires_at,
            "access_method": config["template_vm_access_method"],
            "pool": config["pool_name"],
            "template_vm_username": config["template_vm_username"],
            "template_vm_password": config["template_vm_password"],
        }
        provision.append_pool_entries(config["url_output_file"], [entry])
        reload_pool()
        set_ticket(ticket, status="ready", url=url)
    except Exception as exc:
        set_ticket(ticket, status="error", detail=str(exc))


@app.route("/api/redeem", methods=["POST"])
@limiter.limit("10/minute")
def redeem():
    body = request.get_json(silent=True) or {}
    config = find_pool_by_code(body.get("code"))
    if config is None:
        return jsonify(detail="Unknown code."), 404

    pool_name = config_pool_name(config)
    with lock:
        available = [entry for entry in pool if is_available(entry) and display_name(entry) == pool_name]
        if available:
            entry = random.choice(available)
            entry["claimed"] = True
            try:
                entry["url"] = provision.mint_session_url(entry)
            except Exception as exc:
                entry["claimed"] = False
                save_pool()
                return jsonify(detail=f"VM is not reachable right now ({exc}). Please try again."), 502
            save_pool()
            return jsonify(status="ready", url=entry["url"], pool=pool_name)

    ticket = str(uuid.uuid4())
    with lock:
        redeem_tickets[ticket] = {"status": "provisioning", "stage": "cloning"}
    threading.Thread(target=run_redeem_provision, args=(ticket, config), daemon=True).start()
    return jsonify(status="provisioning", ticket=ticket, pool=pool_name), 202


@app.route("/api/redeem/<ticket>", methods=["GET"])
@limiter.limit("60/minute")
def redeem_status(ticket):
    with lock:
        snapshot = dict(redeem_tickets[ticket]) if ticket in redeem_tickets else None
    if snapshot is None:
        return jsonify(detail="Unknown ticket."), 404
    return jsonify(snapshot)


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
        if not auth or not hmac.compare_digest(auth.password, ADMIN_PASSWORD):
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
                "pool": display_name(entry),
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

    if not config.get("pool_code"):
        config["pool_code"] = generate_pool_code()

    configs = load_configs()
    configs[config_pool_name(config)] = config
    save_configs(configs)

    job = start_job("provision", provision.run_parallel_provisioning, config, count)
    if job is None:
        return jsonify(detail="A job is already running."), 409
    return jsonify(job_id=job["id"])


@app.route("/api/admin/pools")
@admin_required
def admin_pools():
    configs = load_configs()
    with lock:
        available = {}
        for entry in pool:
            if is_available(entry):
                name = display_name(entry)
                available[name] = available.get(name, 0) + 1
    pools = [
        {
            "name": name,
            "available": available.get(name, 0),
            "count": cfg.get("vm_count", 5),
            "config": {k: v for k, v in cfg.items() if k not in provision.SECRET_FIELDS},
        }
        for name, cfg in sorted(configs.items())
    ]
    return jsonify(pools)


@app.route("/api/admin/pools/<path:name>", methods=["PUT"])
@admin_required
def admin_update_pool(name):
    body = request.get_json(silent=True) or {}
    configs = load_configs()
    saved = configs.get(name)
    if saved is None:
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404

    overrides = {k: v for k, v in body.items() if k != "pool_name" and v not in (None, "")}
    try:
        config = provision.build_config({**saved, **overrides})
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    configs[name] = config
    save_configs(configs)
    return jsonify({k: v for k, v in config.items() if k not in provision.SECRET_FIELDS})


@app.route("/api/admin/pools/<path:name>", methods=["DELETE"])
@admin_required
def admin_delete_pool(name):
    configs = load_configs()
    if name not in configs:
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404
    del configs[name]
    save_configs(configs)
    return jsonify(deleted=name)


@app.route("/api/admin/pool-code", methods=["POST"])
@admin_required
def admin_pool_code():
    body = request.get_json(silent=True) or {}
    name = body.get("pool")
    code = (body.get("code") or "").strip().upper()
    configs = load_configs()
    if name not in configs:
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404
    configs[name]["pool_code"] = code
    save_configs(configs)
    return jsonify(pool=name, code=code)


@app.route("/api/admin/redeploy", methods=["POST"])
@admin_required
def admin_redeploy():
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    configs = load_configs()
    config = configs.get(name)
    if config is None:
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404

    count = body.get("count")
    try:
        count = int(count) if count not in (None, "") else config["vm_count"]
    except (ValueError, TypeError):
        return jsonify(detail="count must be an integer."), 400
    if count < 1:
        return jsonify(detail="count must be at least 1."), 400

    config["vm_count"] = count
    save_configs(configs)
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
    with job_lock:
        if current_job is None or (job_id and current_job["id"] != job_id):
            return jsonify(detail="No such job."), 404
        job_snapshot = {k: v for k, v in current_job.items()}
    return jsonify(job_snapshot)


if __name__ == "__main__":
    print(f"Loaded {len(pool)} VM(s) from {POOL_FILE}")
    threading.Thread(target=watch_pool_file, daemon=True).start()
    if REAP_INTERVAL_SECONDS > 0:
        threading.Thread(target=reap_expired_vms, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
