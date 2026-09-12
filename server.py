import hmac
import json
import os
import random
import secrets
import threading
import time
import uuid
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import applog
import destroy
import poolstore
import provision

load_dotenv()

BASE_DIR = Path(__file__).parent
POOL_FILE = BASE_DIR / "pool.json"
CONFIGS_FILE = BASE_DIR / "configs.json"
REAP_INTERVAL_SECONDS = 60
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])
ADMIN_LIMIT = "60/minute"

ticket_lock = threading.Lock()
job_lock = threading.Lock()
jobs = {"provision": None, "destroy": None}


@app.errorhandler(RateLimitExceeded)
def rate_limited(exc):
    return jsonify(detail="Too many requests. Please wait a minute and try again."), 429


def is_expired(entry):
    return entry.get("expires_at") is not None and time.time() >= entry["expires_at"]


def is_available(entry):
    return not entry["claimed"] and not entry.get("reserved") and not is_expired(entry)


def os_type(entry):
    """rdp is Windows; ssh/vnc/unset is Linux."""
    return "windows" if entry.get("access_method") == "rdp" else "linux"


def display_name(entry):
    return entry.get("pool") or ("Windows" if entry.get("access_method") == "rdp" else "Linux")


def load_configs():
    if not CONFIGS_FILE.exists():
        return {}
    with open(CONFIGS_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def entry_ttl(entry):
    config = load_configs().get(entry.get("pool"))
    return (config or {}).get("guac_link_ttl_seconds", 7200)


def config_pool_name(config):
    return config.get("pool_name") or ("Windows" if config.get("template_vm_access_method") == "rdp" else "Linux")


CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_pool_code(used):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        if code not in used:
            return code


def gated_pools():
    return {name for name, cfg in load_configs().items() if cfg.get("pool_code")}


def unreserve(entries, vmid):
    for entry in entries:
        if entry["vmid"] == vmid and entry.get("reserved"):
            del entry["reserved"]
    return entries


def remove_dead_entry(entries, vmid):
    return [e for e in entries if e["vmid"] != vmid]


def finalize_reservation(entries, vmid, url, state):
    for entry in entries:
        if entry["vmid"] == vmid and entry.get("reserved"):
            del entry["reserved"]
            entry["claimed"] = True
            entry["url"] = url
            state["done"] = True
    return entries


def mint_or_prune(entry, ttl, pool_predicate, log_prefix):
    """Mint a session URL for entry. If its VM was removed externally, prune
    it from pool.json and retry once against another matching entry."""
    current = entry
    for _ in range(2):
        try:
            return provision.mint_session_url(current, ttl), current
        except provision.VMNotFoundError:
            applog.log.info(
                f"{log_prefix}: VM {current['vmid']} not found on Proxmox "
                f"(removed externally); pruned from pool.json"
            )
            state = {}

            def prune_and_pick(entries, cur=current, state=state):
                entries = remove_dead_entry(entries, cur["vmid"])
                candidates = [e for e in entries if pool_predicate(e)]
                if candidates:
                    nxt = random.choice(candidates)
                    nxt["reserved"] = True
                    state["next"] = nxt
                return entries

            poolstore.update(POOL_FILE, prune_and_pick)
            if "next" not in state:
                return None, None
            current = state["next"]
        except Exception as exc:
            applog.log.info(f"{log_prefix}: mint failed for VM {current['vmid']}: {exc}")
            poolstore.update(POOL_FILE, lambda es: unreserve(es, current["vmid"]))
            return None, None
    poolstore.update(POOL_FILE, lambda es: unreserve(es, current["vmid"]))
    return None, None


GHOST_SWEEP_GRACE_SECONDS = 120


def proxmox_target_for_pool(pool_name):
    config = load_configs().get(pool_name) or {}
    return provision.build_config(config)


def sweep_ghost_vms():
    """Prunes pool.json entries whose VM was removed from Proxmox externally."""
    configs = load_configs()
    entries = poolstore.load(POOL_FILE)
    eligible = [e for e in entries if e.get("pool") in configs and time.time() - e.get("created_at", 0) >= GHOST_SWEEP_GRACE_SECONDS]

    by_target = {}
    for entry in eligible:
        config = proxmox_target_for_pool(entry.get("pool"))
        key = (config["proxmox_host"], config["proxmox_node"])
        by_target.setdefault(key, (config, []))[1].append(entry)

    ghost_vmids = set()
    for (host, node), (config, target_entries) in by_target.items():
        try:
            proxmox = destroy.get_proxmox_client(config)
            live_vmids = {vm["vmid"] for vm in destroy.list_workshop_vms(proxmox, node)}
        except Exception as exc:
            applog.log.info(f"Sweep: could not list live VMs for {host}/{node}: {exc}; skipping this target")
            continue
        for entry in target_entries:
            if entry["vmid"] not in live_vmids:
                ghost_vmids.add(entry["vmid"])

    if not ghost_vmids:
        return
    for vmid in ghost_vmids:
        applog.log.info(f"Sweep: VM {vmid} not found on Proxmox (removed externally); pruned from pool.json")
    poolstore.update(POOL_FILE, lambda es: [e for e in es if e["vmid"] not in ghost_vmids])
    applog.log.info(f"Sweep: pruned {len(ghost_vmids)} ghost VM(s) from pool.json")


def reap_expired_vms():
    """Destroys expired VMs; run_teardown also prunes them from pool.json."""
    while True:
        time.sleep(REAP_INTERVAL_SECONDS)
        try:
            entries = poolstore.load(POOL_FILE)
        except (OSError, json.JSONDecodeError):
            continue
        with job_lock:
            job_running = any(j and j["status"] == "running" for j in jobs.values())
        if job_running:
            applog.log.info("Reaper: skipping cycle, an admin job is running")
            continue
        if any(is_expired(entry) for entry in entries):
            try:
                results = destroy.run_teardown(destroy.build_config(), mode="expired")
                applog.log.info(f"Reaper: destroyed {len(results)} expired VM(s)")
            except Exception as exc:
                applog.log.info(f"Reaper: teardown failed, will retry next cycle: {exc}")
        try:
            sweep_ghost_vms()
        except Exception as exc:
            applog.log.info(f"Sweep: failed, will retry next cycle: {exc}")


@app.route("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.route("/claim/<path:label>")
def claim_link(label):
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.route("/style.css")
def style():
    return send_from_directory(BASE_DIR / "static", "style.css")


@app.route("/scripts/<path:filename>")
def scripts(filename):
    return send_from_directory(BASE_DIR / "static" / "scripts", filename)


@app.route("/images/<path:filename>")
def images(filename):
    return send_from_directory(BASE_DIR / "images", filename)


@app.route("/api/types")
def types():
    pools = {name: {"name": name, "available": 0, "dispenser": bool(config.get("dispenser")), "private": bool(config.get("private"))} for name, config in load_configs().items()}
    for entry in poolstore.load(POOL_FILE):
        if not is_available(entry):
            continue
        name = display_name(entry)
        group = pools.setdefault(name, {"name": name, "available": 0, "dispenser": False, "private": False})
        group["available"] += 1
    return jsonify(pools=sorted(pools.values(), key=lambda p: p["name"]), coded=len(gated_pools()))


@app.route("/api/claim", methods=["POST"])
@limiter.limit("10/minute")
def claim():
    body = request.get_json(silent=True) or {}
    requested_pool = body.get("pool")
    requested_os = body.get("os")

    configs = load_configs()
    if requested_pool:
        config = configs.get(requested_pool)
        if config and config.get("private") and not code_matches(config, body.get("code")):
            return jsonify(detail="This pool requires a valid code.", code_required=True), 403
    private_pools = {name for name, cfg in configs.items() if cfg.get("private")}

    state = {}

    def reserve(entries):
        available = [entry for entry in entries if is_available(entry)]
        if requested_pool:
            available = [entry for entry in available if display_name(entry) == requested_pool]
        else:
            available = [entry for entry in available if display_name(entry) not in private_pools]
            if requested_os:
                available = [entry for entry in available if os_type(entry) == requested_os]
        if available:
            entry = random.choice(available)
            entry["reserved"] = True
            state["entry"] = entry
        return entries

    poolstore.update(POOL_FILE, reserve)
    if "entry" not in state:
        config = configs.get(requested_pool or "")
        if not config or not config.get("dispenser"):
            if requested_pool and requested_pool not in configs:
                return jsonify(detail=f"Unknown pool {requested_pool!r}."), 404
            if requested_pool:
                return jsonify(detail=f"No VMs available in pool {requested_pool!r} right now."), 404
            return jsonify(detail="No VMs available right now. Please contact your instructor."), 404
        ticket = str(uuid.uuid4())
        with ticket_lock:
            redeem_tickets[ticket] = {"status": "provisioning", "stage": "cloning"}
        threading.Thread(target=run_redeem_provision, args=(ticket, config), daemon=True).start()
        return jsonify(status="provisioning", ticket=ticket, pool=requested_pool), 202

    entry = state["entry"]

    def predicate(e):
        if not is_available(e):
            return False
        if requested_pool:
            return display_name(e) == requested_pool
        return not requested_os or os_type(e) == requested_os

    url, final_entry = mint_or_prune(entry, entry_ttl(entry), predicate, "Claim")
    if url is None:
        return jsonify(detail="VM is not reachable right now. Please contact your instructor."), 502

    poolstore.update(POOL_FILE, lambda es: finalize_reservation(es, final_entry["vmid"], url, state))
    return jsonify(url=url, expires_at=final_entry.get("expires_at"))


@app.route("/api/validate", methods=["POST"])
@limiter.limit("30/minute")
def validate():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    entries = poolstore.load(POOL_FILE)
    entry = next((e for e in entries if e.get("url") == url), None)
    if entry is None:
        return jsonify(valid=False)
    if not entry.get("claimed"):
        return jsonify(valid=False, expired=is_expired(entry))
    if not is_expired(entry):
        return jsonify(valid=True, expires_at=entry.get("expires_at"))

    state = {}

    def replace_expired(current):
        expired = next((e for e in current if e.get("url") == url and e.get("claimed")), None)
        if expired is None:
            state["gone"] = True
            return current
        expired["claimed"] = False
        available = [e for e in current if is_available(e) and display_name(e) == display_name(expired)]
        if not available:
            available = [e for e in current if is_available(e) and os_type(e) == os_type(expired)]
        if available:
            replacement = random.choice(available)
            replacement["reserved"] = True
            state["entry"] = replacement
        return current

    poolstore.update(POOL_FILE, replace_expired)
    if state.get("gone"):
        return jsonify(valid=False)
    if "entry" not in state:
        return jsonify(valid=False, expired=True)

    replacement = state["entry"]
    expired_name = display_name(replacement)
    expired_os = os_type(replacement)

    def predicate(e):
        return is_available(e) and (display_name(e) == expired_name or os_type(e) == expired_os)

    fresh, final_entry = mint_or_prune(replacement, entry_ttl(replacement), predicate, "Validate")
    if fresh is None:
        return jsonify(valid=False, expired=True, detail="Could not prepare a replacement machine. Please try again.")

    poolstore.update(POOL_FILE, lambda es: finalize_reservation(es, final_entry["vmid"], fresh, state))
    if not state.get("done"):
        return jsonify(valid=False, expired=True)
    return jsonify(valid=False, expired=True, url=fresh, expires_at=final_entry.get("expires_at"))


@app.route("/api/reconnect", methods=["POST"])
@limiter.limit("10/minute")
def reconnect():
    """Stored ?token= URLs idle out (~60min), so re-mint a fresh session with the VM's current IP."""
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    entry = next((e for e in poolstore.load(POOL_FILE) if e.get("url") == url), None)
    if entry is None:
        return jsonify(valid=False), 404
    if not entry.get("claimed"):
        if is_expired(entry):
            return jsonify(valid=False, expired=True), 410
        return jsonify(valid=False), 404
    if is_expired(entry):
        return jsonify(valid=False, expired=True), 410
    try:
        fresh = provision.mint_session_url(entry, entry_ttl(entry))
    except provision.VMNotFoundError:
        applog.log.info(
            f"Reconnect: VM {entry['vmid']} not found on Proxmox "
            f"(removed externally); pruned from pool.json"
        )
        poolstore.update(POOL_FILE, lambda es: remove_dead_entry(es, entry["vmid"]))
        return jsonify(valid=False, expired=True)
    except Exception as exc:
        applog.log.info(f"Reconnect: mint failed for VM {entry['vmid']}: {exc}")
        return jsonify(valid=False, detail="VM is not reachable right now. Please try again."), 502

    state = {}

    def finalize(entries):
        for e in entries:
            if e.get("url") == url and e.get("claimed"):
                e["url"] = fresh
                state["done"] = True
        return entries

    poolstore.update(POOL_FILE, finalize)
    if not state.get("done"):
        return jsonify(valid=False), 404
    return jsonify(valid=True, url=fresh, expires_at=entry.get("expires_at"))


@app.route("/api/release", methods=["POST"])
@limiter.limit("10/minute")
def release():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "")
    state = {}

    def mark_released(entries):
        for entry in entries:
            if entry.get("url") == url and entry.get("claimed"):
                entry["expires_at"] = time.time()
                state["entry"] = entry
        return entries

    poolstore.update(POOL_FILE, mark_released)
    if "entry" not in state:
        return jsonify(detail="No assigned machine found."), 404
    entry = state["entry"]

    def destroy_released():
        try:
            config = destroy.build_config()
            proxmox = destroy.get_proxmox_client(config)
            destroy.destroy_worker(proxmox, config["proxmox_node"], entry["vmid"], f"workshop-{entry.get('student_id')}", applog.log.info)
        except Exception as exc:
            applog.log.info(f"Release: destroying VM {entry['vmid']} failed, marked expired for reaper retry: {exc}")
            return
        poolstore.update(POOL_FILE, lambda es: [e for e in es if e.get("vmid") != entry["vmid"]])

    threading.Thread(target=destroy_released, daemon=True).start()
    return jsonify(released=True)


redeem_tickets = {}


def drop_ticket(ticket):
    with ticket_lock:
        redeem_tickets.pop(ticket, None)


def set_ticket(ticket, **fields):
    with ticket_lock:
        if ticket in redeem_tickets:
            redeem_tickets[ticket].update(fields)
            status = fields.get("status")
            if status in ("ready", "error"):
                retention = 86400 if status == "ready" else 600
                threading.Timer(retention, drop_ticket, args=(ticket,)).start()


def code_matches(config, code):
    code = (code or "").strip().upper()
    if not code or not code.isascii():
        return False
    stored = (config.get("pool_code") or "").strip().upper()
    return bool(stored) and hmac.compare_digest(stored, code)


def find_pool_by_code(code):
    for config in load_configs().values():
        if code_matches(config, code):
            return config
    return None


def run_redeem_provision(ticket, config):
    vmid = None

    def log(message):
        applog.log.info(message)
        text = str(message).lower()
        if "booting" in text:
            set_ticket(ticket, stage="booting")
        elif "waiting for ip" in text:
            set_ticket(ticket, stage="network")

    try:
        config = provision.build_config(config)
        student_id = provision.allocate_student_ids(config["pool_name"], config["url_output_file"], 1)[0]
        vmid, student_id, url, expires_at = provision.provision_one(config, student_id, log)
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
            "created_at": time.time(),
        }
        provision.append_pool_entries(config["url_output_file"], [entry])
        set_ticket(ticket, status="ready", url=url, expires_at=expires_at)
    except Exception as exc:
        applog.log.info(f"Redeem provisioning failed: {exc}")
        if vmid is not None:
            try:
                dconfig = destroy.build_config()
                proxmox = destroy.get_proxmox_client(dconfig)
                destroy.destroy_worker(proxmox, dconfig["proxmox_node"], vmid, f"workshop-{student_id}", applog.log.info)
            except Exception as cleanup_exc:
                applog.log.info(f"Redeem cleanup: destroying VM {vmid} failed: {cleanup_exc}")
        set_ticket(ticket, status="error", detail="Provisioning failed. Please try again.")


@app.route("/api/redeem", methods=["POST"])
@limiter.limit("10/minute")
def redeem():
    body = request.get_json(silent=True) or {}
    config = find_pool_by_code(body.get("code"))
    if config is None:
        return jsonify(detail="Unknown code."), 404

    pool_name = config_pool_name(config)
    state = {}

    def reserve(entries):
        available = [entry for entry in entries if is_available(entry) and display_name(entry) == pool_name]
        if available:
            entry = random.choice(available)
            entry["reserved"] = True
            state["entry"] = entry
        return entries

    poolstore.update(POOL_FILE, reserve)
    if "entry" in state:
        entry = state["entry"]

        def predicate(e):
            return is_available(e) and display_name(e) == pool_name

        url, final_entry = mint_or_prune(entry, entry_ttl(entry), predicate, "Redeem")
        if url is None:
            return jsonify(detail="VM is not reachable right now. Please try again."), 502
        poolstore.update(POOL_FILE, lambda es: finalize_reservation(es, final_entry["vmid"], url, state))
        return jsonify(status="ready", url=url, pool=pool_name, expires_at=final_entry.get("expires_at"))

    ticket = str(uuid.uuid4())
    with ticket_lock:
        redeem_tickets[ticket] = {"status": "provisioning", "stage": "cloning"}
    threading.Thread(target=run_redeem_provision, args=(ticket, config), daemon=True).start()
    return jsonify(status="provisioning", ticket=ticket, pool=pool_name), 202


@app.route("/api/redeem/<ticket>", methods=["GET"])
@limiter.limit("60/minute")
def redeem_status(ticket):
    with ticket_lock:
        snapshot = dict(redeem_tickets[ticket]) if ticket in redeem_tickets else None
    if snapshot is None:
        return jsonify(detail="Unknown ticket."), 404
    return jsonify(snapshot)


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
        applog.log.info(message)
        job["log"].append(str(message))
    return append


def start_job(kind, target, *target_args):
    """Runs target(*target_args, log=...) in a background thread. One job per kind."""
    with job_lock:
        job = jobs[kind]
        if job is not None and job["status"] == "running":
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
        jobs[kind] = job

    def runner():
        try:
            target(*target_args, log=job_log_appender(job))
            job["status"] = "done"
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            applog.log.error(f"Job failed: {exc}")
            job["log"].append(f"❌ Job failed: {exc}")
        finally:
            job["finished_at"] = time.time()

    threading.Thread(target=runner, daemon=True).start()
    return job


@app.route("/admin")
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_page():
    return send_from_directory(BASE_DIR / "static", "admin.html")


@app.route("/api/admin/pool")
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_pool():
    entries = []
    for entry in poolstore.load(POOL_FILE):
        entries.append({
            "vmid": entry.get("vmid"),
            "student_id": entry.get("student_id"),
            "pool": display_name(entry),
            "url": entry.get("url"),
            "claimed": entry.get("claimed") or entry.get("reserved"),
            "expired": is_expired(entry),
            "expires_at": entry.get("expires_at"),
        })
    return jsonify(entries)


@app.route("/api/admin/provision", methods=["POST"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_provision():
    body = request.get_json(silent=True) or {}
    try:
        config = provision.build_config(body)
        provision.require_template_fields(config)
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    count = config["vm_count"]

    def save(configs):
        config["pool_code"] = generate_pool_code({c.get("pool_code") for c in configs.values()})
        configs[config_pool_name(config)] = {k: v for k, v in config.items() if k not in provision.GLOBAL_FIELDS}
        return configs

    poolstore.update(CONFIGS_FILE, save, dict)

    job = start_job("provision", provision.run_parallel_provisioning, config, count)
    if job is None:
        return jsonify(detail="A provision job is already running."), 409
    return jsonify(job_id=job["id"])


@app.route("/api/admin/pools")
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_pools():
    configs = load_configs()
    counts = {}
    for entry in poolstore.load(POOL_FILE):
        name = display_name(entry)
        info = counts.setdefault(name, [0, 0])
        info[0] += 1
        if is_available(entry):
            info[1] += 1
    pools = [
        {
            "name": name,
            "available": counts.get(name, [0, 0])[1],
            "total": counts.get(name, [0, 0])[0],
            "count": cfg.get("vm_count", 5),
            "config": {k: v for k, v in cfg.items() if k not in provision.SECRET_FIELDS},
        }
        for name, cfg in sorted(configs.items())
    ]
    return jsonify(pools)


@app.route("/api/admin/pools/<path:name>", methods=["PUT"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_update_pool(name):
    body = request.get_json(silent=True) or {}
    saved = load_configs().get(name)
    if saved is None:
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404
    if body.get("private") and not (saved.get("pool_code") or "").strip():
        return jsonify(detail="Private pools require a pool code."), 400

    overrides = {k: v for k, v in body.items() if k not in ("pool_name", "pool_code") and v not in (None, "")}
    try:
        config = provision.build_config({**saved, **overrides})
        provision.require_template_fields(config)
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    state = {}

    def replace(configs):
        if name in configs:
            configs[name] = {k: v for k, v in config.items() if k not in provision.GLOBAL_FIELDS}
            state["done"] = True
        return configs

    poolstore.update(CONFIGS_FILE, replace, dict)
    if not state.get("done"):
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404
    return jsonify({k: v for k, v in config.items() if k not in provision.SECRET_FIELDS})


@app.route("/api/admin/pools/<path:name>", methods=["DELETE"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_delete_pool(name):
    state = {}

    def remove(configs):
        if name in configs:
            del configs[name]
            state["done"] = True
        return configs

    poolstore.update(CONFIGS_FILE, remove, dict)
    if not state.get("done"):
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404
    return jsonify(deleted=name)


@app.route("/api/admin/redeploy", methods=["POST"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_redeploy():
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    config = load_configs().get(name)
    if config is None:
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404

    count = body.get("count")
    try:
        count = int(count) if count not in (None, "") else config["vm_count"]
    except (ValueError, TypeError):
        return jsonify(detail="count must be an integer."), 400
    if count < 1:
        return jsonify(detail="count must be at least 1."), 400

    state = {}

    def save(configs):
        if name in configs:
            configs[name]["vm_count"] = count
            state["done"] = True
        return configs

    config["vm_count"] = count
    try:
        config = provision.build_config(config)
        provision.require_template_fields(config)
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    poolstore.update(CONFIGS_FILE, save, dict)
    if not state.get("done"):
        return jsonify(detail=f"No saved configuration for pool {name!r}."), 404

    job = start_job("provision", provision.run_parallel_provisioning, config, count)
    if job is None:
        return jsonify(detail="A provision job is already running."), 409
    return jsonify(job_id=job["id"])


@app.route("/api/admin/destroy", methods=["POST"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_destroy():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")
    if mode not in ("all", "specific"):
        return jsonify(detail="mode must be 'all' or 'specific'."), 400

    vmids = body.get("vmids") or []
    if mode == "specific" and not vmids:
        return jsonify(detail="vmids is required when mode is 'specific'."), 400

    config = destroy.build_config()
    job = start_job("destroy", destroy.run_teardown, config, mode, vmids)
    if job is None:
        return jsonify(detail="A destroy job is already running."), 409
    return jsonify(job_id=job["id"])


@app.route("/api/admin/extend", methods=["POST"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_extend():
    body = request.get_json(silent=True) or {}
    hours = body.get("hours")
    if not isinstance(hours, int) or isinstance(hours, bool):
        return jsonify(detail="hours must be an integer."), 400
    if hours < 1:
        return jsonify(detail="hours must be at least 1."), 400
    vmid = body.get("vmid")
    state = {}

    def extend(entries):
        for entry in entries:
            if entry["vmid"] == vmid:
                entry["expires_at"] = max(entry.get("expires_at") or 0, time.time()) + hours * 3600
                state["entry"] = entry
        return entries

    poolstore.update(POOL_FILE, extend)
    if "entry" not in state:
        return jsonify(detail=f"No VM with vmid {vmid!r}."), 404
    return jsonify(vmid=vmid, expires_at=state["entry"]["expires_at"])


@app.route("/api/admin/job")
@app.route("/api/admin/job/<job_id>")
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_job(job_id=None):
    with job_lock:
        candidates = [j for j in jobs.values() if j]
        job = next((j for j in candidates if job_id and j["id"] == job_id), None)
        if job is None and not job_id:
            job = next((j for j in candidates if j["status"] == "running"), None)
            if job is None:
                job = max(candidates, key=lambda j: j["started_at"], default=None)
        if job is None:
            return jsonify(detail="No such job."), 404
        job_snapshot = {k: v for k, v in job.items()}
    return jsonify(job_snapshot)


def startup():
    poolstore.update(POOL_FILE, lambda entries: [{k: v for k, v in e.items() if k != "reserved"} for e in entries])
    poolstore.update(CONFIGS_FILE, lambda configs: {name: {**{k: v for k, v in cfg.items() if k not in provision.GLOBAL_FIELDS}, "dispenser": cfg.get("dispenser", True), "private": bool(cfg.get("private"))} for name, cfg in configs.items()}, dict)
    applog.log.info(f"Loaded {len(poolstore.load(POOL_FILE))} VM(s) from {POOL_FILE}")
    if REAP_INTERVAL_SECONDS > 0:
        threading.Thread(target=reap_expired_vms, daemon=True).start()


startup()

if __name__ == "__main__":
    # gunicorn -w 1 --threads "$(( $(nproc) > 2 ? $(nproc) - 1 : $(nproc) ))" -b 0.0.0.0:5000 server:app
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
