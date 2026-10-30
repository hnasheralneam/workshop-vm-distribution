import hmac
import json
import os
import random
import re
import secrets
import threading
import time
import uuid
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address

import applog
import destroy
import guest
import poolstore
import provision

load_dotenv()

BASE_DIR = Path(__file__).parent
POOL_FILE = BASE_DIR / "pool.json"
CONFIGS_FILE = BASE_DIR / "configs.json"
REAP_INTERVAL_SECONDS = 60
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
MAX_VMS = int(os.getenv("MAX_VMS", "2"))
# Prebind handoff (huitzilopochtli ranked joins): the engine sends a one-time
# join code; the portal runs `huitz join` inside the VM when the player opens
# the link. Disabled unless both sides share HUITZ_PREBIND_SECRET.
PREBIND_SECRET = os.getenv("HUITZ_PREBIND_SECRET", "")
HUITZ_BIN = os.getenv("HUITZ_BIN", "/usr/local/bin/huitz")
# Windows boxes have no `huitz` shim; the zipapp runs through Python.
HUITZ_WIN_PYTHON = os.getenv("HUITZ_WIN_PYTHON", "python")
HUITZ_WIN_AGENT = os.getenv("HUITZ_WIN_AGENT", r"C:\ProgramData\huitzilopochtli\agent.pyz")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
JOIN_CODE_RE = re.compile(r"^[A-Z0-9]{4,64}$")

app = Flask(__name__, static_folder=None)
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])
ADMIN_LIMIT = "60/minute"

ticket_lock = threading.Lock()
job_lock = threading.Lock()
jobs = {"provision": None, "destroy": None}


@app.errorhandler(RateLimitExceeded)
def rate_limited(exc):
    return jsonify(detail="Too many requests. Please wait a minute and try again."), 429


CLAIM_GRACE_SECONDS = 120


def is_expired(entry, grace=0):
    return entry.get("expires_at") is not None and time.time() + grace >= entry["expires_at"]


def is_available(entry):
    return not entry["claimed"] and not entry.get("reserved") and not entry.get("destroying") and not is_expired(entry, CLAIM_GRACE_SECONDS)


def entry_ref(entry):
    return entry.get("pool_code") or entry.get("pool")


def identity_match(entry, uid, url):
    if entry.get("uid"):
        return isinstance(uid, str) and uid == entry["uid"]
    return isinstance(url, str) and bool(url) and url in (entry.get("url"), entry.get("prev_url"))


def find_entry(entries, uid, url):
    return next((e for e in entries if identity_match(e, uid, url)), None)


def load_configs():
    if not CONFIGS_FILE.exists():
        return {}
    with open(CONFIGS_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def entry_ttl(entry):
    config = load_configs().get(entry_ref(entry))
    return (config or {}).get("guac_link_ttl_seconds", 7200)


def config_os(config):
    return "Windows" if config.get("template_vm_access_method") == "rdp" else "Linux"


def config_pool_name(config):
    return config.get("pool_name") or config_os(config)


def ref_label(configs, ref):
    config = configs.get(ref)
    return config_pool_name(config) if config else ref


CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_pool_code(used):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        if code not in used:
            return code


def normalize_code(code):
    return code.strip().upper() if isinstance(code, str) else ""


def json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def prebind_authorized():
    supplied = request.headers.get("X-Huitz-Secret", "")
    return bool(PREBIND_SECRET) and hmac.compare_digest(
        supplied.encode(), PREBIND_SECRET.encode())


def engine_request():
    """Rate-limit exemption for the scoring engine (shared-secret callers).

    A class joining at once makes the engine claim and poll from a single IP;
    the per-IP limits are for browsers, not for one trusted backend."""
    return prebind_authorized()


def parse_prebind(body):
    """Validated prebind state for a claim, or None when not requested."""
    raw = body.get("prebind")
    if not isinstance(raw, dict) or not raw.get("code"):
        return None
    code = str(raw["code"]).strip().upper()
    if not JOIN_CODE_RE.match(code):
        return None
    return {"code": code,
            "back_url": str(raw.get("back_url") or "")[:500],
            "done": False, "error": None, "attempt_at": 0.0}


def open_base():
    return PUBLIC_URL or request.host_url.rstrip("/")


def open_url_for(uid):
    return f"{open_base()}/r/{uid}" if uid else None


def attach_prebind(uid, prebind):
    """Attach (or refresh) the prebind state on the pool entry for uid."""
    if not uid or not prebind:
        return

    def apply(entries):
        for entry in entries:
            if entry.get("uid") == uid:
                current = entry.get("prebind")
                if isinstance(current, dict):
                    current.update(prebind)
                else:
                    entry["prebind"] = dict(prebind)
        return entries

    poolstore.update(POOL_FILE, apply)


def update_prebind(uid, **fields):
    def apply(entries):
        for entry in entries:
            if entry.get("uid") == uid and isinstance(entry.get("prebind"), dict):
                entry["prebind"].update(fields)
        return entries

    poolstore.update(POOL_FILE, apply)


_prebind_locks = {}
_prebind_locks_guard = threading.Lock()


def _prebind_lock(uid):
    """One join attempt per machine at a time (single worker, many threads).

    Two rapid opens of the same link would otherwise both run `huitz join`;
    the loser gets 'already joined' and shows a false error."""
    with _prebind_locks_guard:
        return _prebind_locks.setdefault(uid, threading.Lock())


def prebind_argv(entry, code):
    """The join command for this machine's OS.

    Linux pools (ssh/vnc) use the installed `huitz` shim; Windows pools (rdp)
    have no shim, so the agent zipapp runs through Python."""
    if (entry.get("access_method") or "").lower() == "rdp":
        return [HUITZ_WIN_PYTHON, HUITZ_WIN_AGENT, "join", "--code", code]
    return [HUITZ_BIN, "join", "--code", code]


def run_prebind_join(entry, prebind):
    """Run `huitz join` in the guest once. Returns (ok, detail or None)."""
    now = time.time()
    if now - float(prebind.get("attempt_at") or 0) < 15 and prebind.get("error"):
        return False, prebind["error"]  # don't hammer a VM that just failed
    update_prebind(entry["uid"], attempt_at=now)
    try:
        config = destroy.build_config()
        proxmox = destroy.get_proxmox_client(config)
        status = guest.run(proxmox, config["proxmox_node"], entry["vmid"],
                           prebind_argv(entry, prebind["code"]),
                           timeout_s=90)
    except Exception as exc:
        detail = f"could not reach the VM's guest agent: {exc}"
        update_prebind(entry["uid"], error=detail)
        applog.log.info(f"Prebind {entry['uid']}: {detail}")
        return False, detail
    out = ((status.get("out-data") or "") + (status.get("err-data") or "")).strip()
    if int(status.get("exitcode") or 0) == 0:
        update_prebind(entry["uid"], done=True, error=None)
        applog.log.info(f"Prebind {entry['uid']}: joined")
        return True, None
    detail = out[-400:] or f"huitz join exited {status.get('exitcode')}"
    update_prebind(entry["uid"], error=detail)
    applog.log.info(f"Prebind {entry['uid']} failed: {detail}")
    return False, detail


def unreserve(entries, vmid):
    for entry in entries:
        if entry["vmid"] == vmid and entry.get("reserved"):
            del entry["reserved"]
    return entries


def remove_dead_entry(entries, vmid):
    return [e for e in entries if e["vmid"] != vmid]


def finalize_reservation(entries, vmid, url, state, ttl):
    for entry in entries:
        if entry["vmid"] == vmid and entry.get("reserved"):
            del entry["reserved"]
            entry["claimed"] = True
            entry["url"] = url
            entry["expires_at"] = time.time() + ttl
            if "uid" not in entry:
                entry["uid"] = uuid.uuid4().hex
            state["uid"] = entry["uid"]
            state["expires_at"] = entry["expires_at"]
            state["done"] = True
    return entries


def finalize_claim(final_entry, url, state):
    poolstore.update(POOL_FILE, lambda es: finalize_reservation(es, final_entry["vmid"], url, state, entry_ttl(final_entry)))
    return bool(state.get("done"))


def mint_or_prune(entry, ttl, pool_predicate, log_prefix):
    """Mint a session URL for entry. If its VM was removed externally, prune
    it from pool.json and retry once against another matching entry. Any other
    failure keeps the reservation and is reported as transient."""
    current = entry
    for _ in range(2):
        try:
            return provision.mint_session_url(current, ttl), current, False
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
                return None, None, False
            current = state["next"]
        except Exception as exc:
            applog.log.info(f"{log_prefix}: mint failed for VM {current['vmid']}: {exc}")
            return None, current, True
    return None, current, True


def prune_removed(prefix, entry):
    applog.log.info(f"{prefix}: VM {entry['vmid']} not found on Proxmox (removed externally); pruned from pool.json")
    poolstore.update(POOL_FILE, lambda es: remove_dead_entry(es, entry["vmid"]))


GHOST_SWEEP_GRACE_SECONDS = 120


def proxmox_target_for_pool(ref):
    config = load_configs().get(ref) or {}
    return provision.build_config(config)


def sweep_ghost_vms():
    configs = load_configs()
    entries = poolstore.load(POOL_FILE)
    eligible = [e for e in entries if entry_ref(e) in configs and time.time() - e.get("created_at", 0) >= GHOST_SWEEP_GRACE_SECONDS]

    by_target = {}
    for entry in eligible:
        config = proxmox_target_for_pool(entry_ref(entry))
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

    # 2026-10-30 fix: pool configs can be deleted while dead VMs they once
    # spawned stay tracked in pool.json; those entries are invisible to the
    # per-target sweep above and stick in the UI forever as "expired" ghosts.
    orphaned = [e for e in entries if entry_ref(e) not in configs
                and time.time() - e.get("created_at", 0) >= GHOST_SWEEP_GRACE_SECONDS]
    if orphaned:
        try:
            default_cfg = destroy.build_config()
            proxmox = destroy.get_proxmox_client(default_cfg)
            all_vmids = {int(v["vmid"]) for v in proxmox.nodes(default_cfg["proxmox_node"]).qemu.get()}
            for entry in orphaned:
                if entry.get("vmid") not in all_vmids:
                    ghost_vmids.add(entry["vmid"])
        except Exception as exc:
            applog.log.info(f"Sweep: could not check orphaned-pool entries: {exc}; skipping")


    if not ghost_vmids:
        return
    for vmid in ghost_vmids:
        applog.log.info(f"Sweep: VM {vmid} not found on Proxmox (removed externally); pruned from pool.json")
    poolstore.update(POOL_FILE, lambda es: [e for e in es if e["vmid"] not in ghost_vmids])
    applog.log.info(f"Sweep: pruned {len(ghost_vmids)} ghost VM(s) from pool.json")


def reap_expired_vms():
    """run_teardown also prunes the VMs it destroys from pool.json."""
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


@app.route("/claim")
def claim_page():
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
    configs = load_configs()
    pools = {}
    for code, config in configs.items():
        pools[code] = {"name": config_pool_name(config), "code": None if config.get("private") else code, "available": 0, "dispenser": bool(config.get("dispenser")), "private": bool(config.get("private")), "os": config_os(config)}
    for entry in poolstore.load(POOL_FILE):
        if not is_available(entry) or entry_ref(entry) not in configs:
            continue
        ref = entry_ref(entry)
        group = pools.setdefault(ref, {"name": ref, "code": None, "available": 0, "dispenser": False, "private": False})
        group["available"] += 1
    return jsonify(pools=sorted(pools.values(), key=lambda p: p["name"]), coded=len(configs), max_vms=MAX_VMS)


@app.route("/api/claim", methods=["POST"])
@limiter.limit("10/minute", exempt_when=engine_request)
def claim():
    body = json_body()
    prebind = parse_prebind(body)
    if body.get("prebind") and prebind is None:
        return jsonify(detail="Invalid prebind payload."), 400
    if prebind and not prebind_authorized():
        return jsonify(detail="Prebind is disabled: HUITZ_PREBIND_SECRET is not set on the portal."), 403
    if body.get("code"):
        config = find_pool_by_code(body.get("code"))
        if config is None:
            return jsonify(detail="Unknown code."), 404
        return claim_from_pool(config, prebind)

    configs = load_configs()
    public_refs = {code for code, cfg in configs.items() if not cfg.get("private")}

    state = {}

    def reserve(entries):
        available = [entry for entry in entries if is_available(entry) and entry_ref(entry) in public_refs]
        if available:
            entry = random.choice(available)
            entry["reserved"] = True
            state["entry"] = entry
        return entries

    poolstore.update(POOL_FILE, reserve)
    if "entry" not in state:
        return jsonify(detail="No VMs available right now. Please contact your instructor."), 404

    entry = state["entry"]

    def predicate(e):
        return is_available(e) and entry_ref(e) in public_refs

    url, final_entry, transient = mint_or_prune(entry, entry_ttl(entry), predicate, "Claim")
    if url is None:
        if transient:
            ticket = start_ticket(stage="connecting", prebind=prebind)
            threading.Thread(target=run_mint_retry, args=(ticket, final_entry, predicate, "Claim"), daemon=True).start()
            return jsonify(status="provisioning", stage="connecting", ticket=ticket), 202
        return jsonify(detail="VM is not reachable right now. Please contact your instructor."), 502

    if not finalize_claim(final_entry, url, state):
        return jsonify(detail="That machine was just reclaimed. Please try again."), 503
    if prebind and state.get("uid"):
        attach_prebind(state["uid"], prebind)
    return jsonify(url=url, open_url=open_url_for(state.get("uid")),
                   expires_at=state.get("expires_at"), uid=state.get("uid"))


def provision_running_for(code):
    with job_lock:
        job = jobs["provision"]
        return bool(job and job["status"] == "running" and job.get("pool_code") == code)


def start_ticket(stage="cloning", prebind=None):
    ticket = str(uuid.uuid4())
    info = {"status": "provisioning", "stage": stage}
    if prebind:
        info["prebind"] = prebind
    with ticket_lock:
        prune_tickets_locked(time.time())
        redeem_tickets[ticket] = info
    return ticket


def run_claim_wait(ticket, config):
    code = normalize_code(config.get("pool_code"))
    state = {}
    try:
        while True:
            running = provision_running_for(code)
            state = {}

            def reserve(entries):
                available = [e for e in entries if is_available(e) and entry_ref(e) == code]
                if available:
                    entry = random.choice(available)
                    entry["reserved"] = True
                    state["entry"] = entry
                return entries

            poolstore.update(POOL_FILE, reserve)
            if "entry" in state:
                entry = state["entry"]

                def predicate(e):
                    return is_available(e) and entry_ref(e) == code

                url, final_entry, transient = mint_or_prune(entry, entry_ttl(entry), predicate, "Claim")
                if url is not None and finalize_claim(final_entry, url, state):
                    set_ticket(ticket, status="ready", url=url, expires_at=state.get("expires_at"), uid=state.get("uid"))
                    return
                if transient and final_entry is not None:
                    poolstore.update(POOL_FILE, lambda es: unreserve(es, final_entry["vmid"]))
            if not running:
                break
            time.sleep(3)

        if config.get("dispenser"):
            threading.Thread(target=run_redeem_provision, args=(ticket, config), daemon=True).start()
        else:
            set_ticket(ticket, status="error", detail=f"No VMs available in pool {config_pool_name(config)!r} right now.")
    except Exception as exc:
        applog.log.info(f"Claim wait failed: {exc}")
        if state.get("entry"):
            entry_vmid = state["entry"]["vmid"]
            poolstore.update(POOL_FILE, lambda es: unreserve(es, entry_vmid))
        set_ticket(ticket, status="error", detail="Provisioning failed. Please try again.")


MINT_RETRY_SECONDS = 600


def run_mint_retry(ticket, entry, pool_predicate, log_prefix):
    """Retry minting for a reserved entry in the background after a transient
    failure. Gives up after MINT_RETRY_SECONDS and releases the reservation."""
    vmid = entry["vmid"]
    detail = "VM is not reachable right now. Please contact your instructor."
    try:
        deadline = time.time() + MINT_RETRY_SECONDS
        while time.time() < deadline:
            time.sleep(20)
            current = next((e for e in poolstore.load(POOL_FILE) if e["vmid"] == vmid), None)
            if current is None:
                set_ticket(ticket, status="error", detail=detail)
                return
            try:
                url = provision.mint_session_url(current, entry_ttl(current))
            except provision.VMNotFoundError:
                prune_removed(log_prefix, current)
                set_ticket(ticket, status="error", detail=detail)
                return
            except Exception as exc:
                applog.log.info(f"{log_prefix}: mint retry for VM {vmid}: {exc}")
                continue
            state = {}
            poolstore.update(POOL_FILE, lambda es: finalize_reservation(es, vmid, url, state, entry_ttl(current)))
            if state.get("done"):
                set_ticket(ticket, status="ready", url=url, expires_at=state.get("expires_at"), uid=state.get("uid"))
                return
            set_ticket(ticket, status="error", detail=detail)
            return
        poolstore.update(POOL_FILE, lambda es: unreserve(es, vmid))
        set_ticket(ticket, status="error", detail=detail)
    except Exception as exc:
        applog.log.info(f"{log_prefix}: mint retry failed for VM {vmid}: {exc}")
        poolstore.update(POOL_FILE, lambda es: unreserve(es, vmid))
        set_ticket(ticket, status="error", detail=detail)


def claim_from_pool(config, prebind=None):
    code = normalize_code(config.get("pool_code"))
    pool_name = config_pool_name(config)
    state = {}

    def reserve(entries):
        available = [entry for entry in entries if is_available(entry) and entry_ref(entry) == code]
        if available:
            entry = random.choice(available)
            entry["reserved"] = True
            state["entry"] = entry
        return entries

    poolstore.update(POOL_FILE, reserve)
    if "entry" in state:
        entry = state["entry"]

        def predicate(e):
            return is_available(e) and entry_ref(e) == code

        url, final_entry, transient = mint_or_prune(entry, entry_ttl(entry), predicate, "Claim")
        if url is None:
            if transient:
                ticket = start_ticket(stage="connecting", prebind=prebind)
                threading.Thread(target=run_mint_retry, args=(ticket, final_entry, predicate, "Claim"), daemon=True).start()
                return jsonify(status="provisioning", stage="connecting", ticket=ticket, pool=pool_name), 202
            return jsonify(detail="VM is not reachable right now. Please try again."), 502
        if not finalize_claim(final_entry, url, state):
            return jsonify(detail="That machine was just reclaimed. Please try again."), 503
        if prebind and state.get("uid"):
            attach_prebind(state["uid"], prebind)
        return jsonify(status="ready", url=url, open_url=open_url_for(state.get("uid")),
                       pool=pool_name, expires_at=state.get("expires_at"),
                       uid=state.get("uid"))

    if provision_running_for(code):
        ticket = start_ticket(prebind=prebind)
        threading.Thread(target=run_claim_wait, args=(ticket, config), daemon=True).start()
        return jsonify(status="provisioning", ticket=ticket, pool=pool_name), 202

    if not config.get("dispenser"):
        return jsonify(detail=f"No VMs available in pool {pool_name!r} right now."), 404
    ticket = start_ticket(prebind=prebind)
    threading.Thread(target=run_redeem_provision, args=(ticket, config), daemon=True).start()
    return jsonify(status="provisioning", ticket=ticket, pool=pool_name), 202


@app.route("/api/validate", methods=["POST"])
@limiter.limit("30/minute")
def validate():
    body = json_body()
    uid = body.get("uid")
    url = body.get("url", "")
    entries = poolstore.load(POOL_FILE)
    entry = find_entry(entries, uid, url)
    if entry is None or entry.get("released"):
        return jsonify(valid=False)
    if not entry.get("claimed"):
        return jsonify(valid=False, expired=is_expired(entry))
    if not is_expired(entry):
        try:
            fresh = provision.mint_session_url(entry, entry_ttl(entry))
        except provision.VMNotFoundError:
            prune_removed("Validate", entry)
            return jsonify(valid=False)
        except Exception as exc:
            applog.log.info(f"Validate: mint failed for VM {entry['vmid']}: {exc}")
            return jsonify(valid=True, stale=True, uid=entry.get("uid"), expires_at=entry.get("expires_at"))
        return jsonify(valid=True, url=fresh, uid=entry.get("uid"), expires_at=entry.get("expires_at"))

    state = {}

    def replace_expired(current):
        expired = next((e for e in current if identity_match(e, uid, url) and e.get("claimed") and not e.get("released")), None)
        if expired is None:
            state["gone"] = True
            return current
        expired["claimed"] = False
        ref = entry_ref(expired)
        available = [e for e in current if is_available(e) and entry_ref(e) == ref]
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
    ref = entry_ref(replacement)

    def predicate(e):
        return is_available(e) and entry_ref(e) == ref

    fresh, final_entry, transient = mint_or_prune(replacement, entry_ttl(replacement), predicate, "Validate")
    if fresh is None:
        if transient and final_entry is not None:
            poolstore.update(POOL_FILE, lambda es: unreserve(es, final_entry["vmid"]))
        return jsonify(valid=False, expired=True, detail="Could not prepare a replacement machine. Please try again.")

    poolstore.update(POOL_FILE, lambda es: finalize_reservation(es, final_entry["vmid"], fresh, state, entry_ttl(final_entry)))
    if not state.get("done"):
        return jsonify(valid=False, expired=True)
    return jsonify(valid=False, expired=True, url=fresh, expires_at=state.get("expires_at"), uid=state.get("uid"))


@app.route("/api/reconnect", methods=["POST"])
@limiter.limit("10/minute")
def reconnect():
    """Stored ?token= URLs idle out (~60min), so re-mint a fresh session with the VM's current IP."""
    body = json_body()
    uid = body.get("uid")
    url = body.get("url", "")
    entry = find_entry(poolstore.load(POOL_FILE), uid, url)
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
        prune_removed("Reconnect", entry)
        return jsonify(valid=False, expired=True)
    except Exception as exc:
        applog.log.info(f"Reconnect: mint failed for VM {entry['vmid']}: {exc}")
        return jsonify(valid=False, detail="VM is not reachable right now. Please try again."), 502

    state = {}

    def finalize(entries):
        for e in entries:
            if identity_match(e, uid, url) and e.get("claimed"):
                e["prev_url"] = e.get("url")
                e["url"] = fresh
                state["done"] = True
        return entries

    poolstore.update(POOL_FILE, finalize)
    if not state.get("done"):
        return jsonify(valid=False), 404
    return jsonify(valid=True, url=fresh, uid=entry.get("uid"), expires_at=entry.get("expires_at"))


@app.route("/api/release", methods=["POST"])
@limiter.limit("10/minute")
def release():
    body = json_body()
    uid = body.get("uid")
    url = body.get("url", "")
    state = {}

    def mark_released(entries):
        for entry in entries:
            if identity_match(entry, uid, url) and entry.get("claimed"):
                entry["expires_at"] = time.time()
                entry["released"] = True
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
ticket_drop_at = {}


def prune_tickets_locked(now):
    for ticket in [t for t, at in ticket_drop_at.items() if at <= now]:
        redeem_tickets.pop(ticket, None)
        ticket_drop_at.pop(ticket, None)


def set_ticket(ticket, **fields):
    with ticket_lock:
        if ticket in redeem_tickets:
            redeem_tickets[ticket].update(fields)
            status = fields.get("status")
            if status in ("ready", "error"):
                retention = 86400 if status == "ready" else 600
                ticket_drop_at[ticket] = time.time() + retention


def find_pool_by_code(code):
    code = normalize_code(code)
    if not code:
        return None
    return load_configs().get(code)


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
        pool_name = config_pool_name(config)
        student_id = provision.allocate_student_ids(config, 1)[0]
        vmid, student_id, url, expires_at = provision.provision_one(config, student_id, log)
        set_ticket(ticket, stage="adding")
        entry = {
            "vmid": vmid,
            "student_id": student_id,
            "uid": uuid.uuid4().hex,
            "url": url,
            "claimed": True,
            "expires_at": expires_at,
            "access_method": config["template_vm_access_method"],
            "pool_code": normalize_code(config.get("pool_code")),
            "template_vm_username": config["template_vm_username"],
            "template_vm_password": config["template_vm_password"],
            "created_at": time.time(),
        }
        provision.append_pool_entries(config["url_output_file"], [entry])
        set_ticket(ticket, status="ready", url=url, expires_at=expires_at, uid=entry["uid"])
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
    body = json_body()
    config = find_pool_by_code(body.get("code"))
    if config is None:
        return jsonify(detail="Unknown code."), 404
    return claim_from_pool(config)


@app.route("/api/redeem/<ticket>", methods=["GET"])
@limiter.limit("60/minute", exempt_when=engine_request)
def redeem_status(ticket):
    pending_attach = None
    with ticket_lock:
        prune_tickets_locked(time.time())
        info = redeem_tickets.get(ticket)
        snapshot = dict(info) if info is not None else None
        if info is not None and info.get("status") == "ready":
            prebind = info.get("prebind")
            if prebind and not info.get("prebind_attached"):
                info["prebind_attached"] = True
                pending_attach = (info.get("uid"), dict(prebind))
    if snapshot is None:
        return jsonify(detail="Unknown ticket."), 404
    # Never hand the join code back to ticket holders.
    snapshot.pop("prebind", None)
    snapshot.pop("prebind_attached", None)
    if snapshot.get("status") == "ready" and snapshot.get("uid"):
        if pending_attach:
            attach_prebind(*pending_attach)
        snapshot["open_url"] = open_url_for(snapshot["uid"])
    return jsonify(snapshot)


@app.route("/api/prebind", methods=["POST"])
@limiter.limit("30/minute")
def prebind_update():
    """Rotate the join code the portal holds for an already-claimed VM."""
    if not prebind_authorized():
        return jsonify(detail="Prebind is disabled: HUITZ_PREBIND_SECRET is not set on the portal."), 403
    body = json_body()
    uid = str(body.get("uid") or "")
    code = str(body.get("code") or "").strip().upper()
    if not uid or not JOIN_CODE_RE.match(code):
        return jsonify(detail="uid and a valid one-time code are required."), 400
    entry = next((e for e in poolstore.load(POOL_FILE) if e.get("uid") == uid), None)
    if entry is None:
        return jsonify(detail="No machine with that uid."), 404
    if (entry.get("prebind") or {}).get("done"):
        return jsonify(ok=True, ignored="already joined")
    attach_prebind(uid, {"code": code,
                         "back_url": str(body.get("back_url") or "")[:500],
                         "done": False, "error": None, "attempt_at": 0.0})
    applog.log.info(f"Prebind {uid}: code rotated")
    return jsonify(ok=True)


@app.route("/api/open/<uid>", methods=["POST"])
@limiter.limit("30/minute")
def open_vm(uid):
    """Mint a fresh session for a claimed VM and join it if not yet joined."""
    entry = next((e for e in poolstore.load(POOL_FILE)
                  if e.get("uid") == uid and not e.get("released")), None)
    if entry is None:
        return jsonify(valid=False, detail="This VM is no longer available."), 404
    prebind = entry.get("prebind") if isinstance(entry.get("prebind"), dict) else {}
    if not entry.get("claimed") or is_expired(entry):
        return jsonify(valid=False, expired=True,
                       detail="This VM has expired. Go back to the competition page and get a new one.",
                       back_url=prebind.get("back_url", "")), 410
    try:
        ttl = entry_ttl(entry)
        url = provision.mint_session_url(entry, ttl)
    except Exception as exc:
        applog.log.info(f"Open {uid}: mint failed: {exc}")
        return jsonify(valid=False, detail="Could not reach the VM. Try again in a moment.",
                       back_url=prebind.get("back_url", "")), 502

    def refresh(entries):
        for candidate in entries:
            if candidate.get("uid") == uid:
                # Re-mint the session, not the machine's lifetime: reopening a
                # link must not keep a box alive past its claim TTL.
                candidate["url"] = url
        return entries

    poolstore.update(POOL_FILE, refresh)
    joined, detail = True, None
    if prebind and not prebind.get("done") and prebind.get("code"):
        with _prebind_lock(uid):
            current = next((e for e in poolstore.load(POOL_FILE)
                            if e.get("uid") == uid), entry)
            current_prebind = (current.get("prebind")
                               if isinstance(current.get("prebind"), dict) else {})
            if current_prebind.get("done"):
                joined, detail = True, None
            else:
                joined, detail = run_prebind_join(current, current_prebind)
            prebind = current_prebind or prebind
    return jsonify(valid=True, url=url, joined=joined, detail=detail,
                   back_url=prebind.get("back_url", ""))


OPEN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Starting your VM</title>
<style>
:root{--bg:#181410;--panel:#211b16;--line:#4a3c2e;--fg:#f2e8d8;--muted:#b9a98f;--accent:#c99a62}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:2rem}
main{max-width:34rem;background:var(--panel);border:1px solid var(--line);padding:2rem}
h1{margin:0 0 .75rem;font-size:1.35rem}
p{color:var(--muted);margin:0 0 1rem}
.sub{color:var(--muted);font-size:.85rem;word-break:break-word}
.spin{width:1.1rem;height:1.1rem;border:.15rem solid var(--line);border-top-color:var(--accent);
border-radius:50%;display:inline-block;vertical-align:-.2rem;margin-right:.5rem;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
a.btn{display:inline-block;margin:.4rem .5rem 0 0;padding:.5rem .9rem;border:1px solid var(--accent);
color:var(--accent);text-decoration:none}
a.btn:hover{background:var(--accent);color:#1f1a14}
</style></head><body><main>
<h1>Starting your VM</h1>
<p id="msg"><span class="spin"></span>Preparing your session…</p>
<p id="sub" class="sub" style="display:none"></p>
<div id="actions"></div>
</main>
<script>
(function(){
  var uid=(__UID__);
  var msg=document.getElementById("msg"), actions=document.getElementById("actions"),
      sub=document.getElementById("sub");
  function show(text, links, detail){
    msg.textContent=text; actions.textContent="";
    if(detail){ sub.textContent=detail; sub.style.display="block"; }
    else{ sub.textContent=""; sub.style.display="none"; }
    (links||[]).forEach(function(l){
      if(!l.href) return;
      var a=document.createElement("a"); a.className="btn";
      a.href=l.href; a.textContent=l.text; actions.appendChild(a);
    });
  }
  fetch("/api/open/"+encodeURIComponent(uid),{method:"POST",headers:{"Accept":"application/json"}})
    .then(function(r){return r.json().then(function(d){return d;}).catch(function(){return {};});})
    .then(function(d){
      if(d.valid&&d.url&&d.joined){ location.replace(d.url); return; }
      if(d.valid&&d.url){
        show("The VM itself is ready — you can open it now. Reopen this link in a moment to retry the automatic join.",
             [{href:d.url,text:"Open the VM anyway"},{href:d.back_url,text:"Back to the competition"}],
             d.detail||"");
        return;
      }
      show(d.detail||"This VM is no longer available.",
           [{href:d.back_url,text:"Back to the competition"}]);
    })
    .catch(function(){ show("Could not reach the portal. Reload to try again.",[]); });
})();
</script></body></html>"""


@app.route("/r/<uid>")
def open_page(uid):
    """Stable per-VM link: always mints a fresh session, then redirects."""
    page = OPEN_PAGE.replace("(__UID__)", json.dumps(uid))
    return Response(page, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return jsonify(detail="ADMIN_PASSWORD is not set on the server."), 503
        auth = request.authorization
        password = (auth.password or "") if auth else ""
        if not auth or not hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
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


def start_job(kind, target, *target_args, pool_code=None):
    """One job per kind."""
    with job_lock:
        job = jobs[kind]
        if job is not None and job["status"] == "running":
            return None
        job = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "pool_code": pool_code,
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
    configs = load_configs()
    for entry in poolstore.load(POOL_FILE):
        config = configs.get(entry_ref(entry))
        entries.append({
            "vmid": entry.get("vmid"),
            "student_id": entry.get("student_id"),
            "pool": ref_label(configs, entry_ref(entry)),
            "os": config_os(config) if config else None,
            "claimed": entry.get("claimed") or entry.get("reserved"),
            "expired": is_expired(entry),
            "expires_at": entry.get("expires_at"),
        })
    return jsonify(entries)


def template_check_error(config):
    try:
        provision.ensure_template(provision.get_proxmox_client(config), config)
    except Exception as exc:
        return f"Template check failed: {exc}"
    return None


@app.route("/api/admin/provision", methods=["POST"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_provision():
    body = {k: v for k, v in json_body().items() if k not in provision.GLOBAL_FIELDS}
    try:
        config = provision.build_config(body)
        provision.require_template_fields(config)
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    error = template_check_error(config)
    if error:
        return jsonify(detail=error), 400

    count = config["vm_count"]

    def save(configs):
        config["modified_at"] = time.time()
        config["pool_code"] = generate_pool_code({normalize_code(c.get("pool_code")) for c in configs.values()})
        configs[config["pool_code"]] = {k: v for k, v in config.items() if k not in provision.GLOBAL_FIELDS}
        return configs

    poolstore.update(CONFIGS_FILE, save, dict)

    job = start_job("provision", provision.run_parallel_provisioning, config, count, pool_code=normalize_code(config.get("pool_code")))
    if job is None:
        poolstore.update(CONFIGS_FILE, lambda cs: {k: v for k, v in cs.items() if k != config["pool_code"]}, dict)
        return jsonify(detail="A provision job is already running."), 409
    return jsonify(job_id=job["id"])


def entry_activity(entry, now):
    if (entry.get("claimed") or entry.get("reserved")) and not is_expired(entry):
        return now
    if entry.get("claimed") or entry.get("expires_at"):
        return entry.get("expires_at") or 0
    return entry.get("created_at") or 0


@app.route("/api/admin/pools")
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_pools():
    configs = load_configs()
    now = time.time()
    stats = {}
    for entry in poolstore.load(POOL_FILE):
        ref = entry_ref(entry)
        info = stats.setdefault(ref, {"total": 0, "available": 0, "in_use": 0, "last_used": 0})
        info["total"] += 1
        if is_available(entry):
            info["available"] += 1
        active = (entry.get("claimed") or entry.get("reserved")) and not is_expired(entry)
        if active:
            info["in_use"] += 1
        info["last_used"] = max(info["last_used"], entry_activity(entry, now))
    pools = [
        {
            "name": ref_label(configs, code),
            "code": code,
            "available": stats.get(code, {}).get("available", 0),
            "total": stats.get(code, {}).get("total", 0),
            "in_use": stats.get(code, {}).get("in_use", 0),
            "last_used": stats.get(code, {}).get("last_used", 0),
            "count": cfg.get("vm_count", 5),
            "config": {k: v for k, v in cfg.items() if k not in provision.SECRET_FIELDS},
        }
        for code, cfg in sorted(configs.items(), key=lambda item: ref_label(configs, item[0]))
    ]
    return jsonify(pools)


@app.route("/api/admin/pools/<code>", methods=["PUT"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_update_pool(code):
    body = json_body()
    saved = load_configs().get(code)
    if saved is None:
        return jsonify(detail=f"No saved configuration for pool {code!r}."), 404

    overrides = {k: v for k, v in body.items() if k != "pool_code" and k not in provision.GLOBAL_FIELDS and v not in (None, "")}
    try:
        config = provision.build_config({**saved, **overrides})
        provision.require_template_fields(config)
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    error = template_check_error(config)
    if error:
        return jsonify(detail=error), 400

    new_name = config_pool_name(config)
    if new_name != config_pool_name(saved) and any(ref != code and config_pool_name(other) == new_name for ref, other in load_configs().items()):
        return jsonify(detail=f"A pool named {new_name!r} already exists."), 400

    state = {}

    def replace(configs):
        if code in configs:
            config["modified_at"] = time.time()
            configs[code] = {k: v for k, v in config.items() if k not in provision.GLOBAL_FIELDS}
            state["done"] = True
        return configs

    poolstore.update(CONFIGS_FILE, replace, dict)
    if not state.get("done"):
        return jsonify(detail=f"No saved configuration for pool {code!r}."), 404
    return jsonify({k: v for k, v in config.items() if k not in provision.SECRET_FIELDS})


@app.route("/api/admin/pools/<code>", methods=["DELETE"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_delete_pool(code):
    state = {}

    def remove(configs):
        if code in configs:
            del configs[code]
            state["done"] = True
        return configs

    poolstore.update(CONFIGS_FILE, remove, dict)
    if not state.get("done"):
        return jsonify(detail=f"No saved configuration for pool {code!r}."), 404
    return jsonify(deleted=code)


@app.route("/api/admin/redeploy", methods=["POST"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_redeploy():
    body = json_body()
    code = body.get("code")
    config = load_configs().get(code) if isinstance(code, str) else None
    if config is None:
        return jsonify(detail=f"No saved configuration for pool {code!r}."), 404

    count = body.get("count")
    try:
        count = int(count) if count not in (None, "") else config["vm_count"]
    except (ValueError, TypeError):
        return jsonify(detail="count must be an integer."), 400
    if count < 1:
        return jsonify(detail="count must be at least 1."), 400

    state = {}

    def save(configs):
        if code in configs:
            configs[code]["vm_count"] = count
            configs[code]["modified_at"] = time.time()
            state["done"] = True
        return configs

    config["vm_count"] = count
    try:
        config = provision.build_config(config)
        provision.require_template_fields(config)
    except (ValueError, TypeError) as exc:
        return jsonify(detail=f"Invalid configuration: {exc}"), 400

    error = template_check_error(config)
    if error:
        return jsonify(detail=error), 400

    poolstore.update(CONFIGS_FILE, save, dict)
    if not state.get("done"):
        return jsonify(detail=f"No saved configuration for pool {code!r}."), 404

    job = start_job("provision", provision.run_parallel_provisioning, config, count, pool_code=normalize_code(config.get("pool_code")))
    if job is None:
        return jsonify(detail="A provision job is already running."), 409
    return jsonify(job_id=job["id"])


@app.route("/api/admin/destroy", methods=["POST"])
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_destroy():
    body = json_body()
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
    body = json_body()
    hours = body.get("hours")
    if not isinstance(hours, int) or isinstance(hours, bool):
        return jsonify(detail="hours must be an integer."), 400
    if hours < 1:
        return jsonify(detail="hours must be at least 1."), 400
    vmid = body.get("vmid")
    state = {}

    def extend(entries):
        for entry in entries:
            if entry["vmid"] == vmid and not entry.get("destroying"):
                entry["expires_at"] = max(entry.get("expires_at") or 0, time.time()) + hours * 3600
                state["entry"] = entry
        return entries

    poolstore.update(POOL_FILE, extend)
    if "entry" not in state:
        return jsonify(detail=f"No VM with vmid {vmid!r}, or it is being destroyed."), 404
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


@app.route("/api/admin/logs")
@limiter.limit(ADMIN_LIMIT)
@admin_required
def admin_logs():
    try:
        with open(applog.LOG_FILE, errors="replace") as f:
            lines = f.readlines()[-1000:]
    except OSError:
        lines = []
    return jsonify(lines=[re.sub(r"\x1b\[[0-9;]*m", "", line.rstrip("\n")) for line in lines])


def normalize_configs(configs):
    normalized = {}
    used = set()
    for cfg in configs.values():
        cfg = {**{k: v for k, v in cfg.items() if k not in provision.GLOBAL_FIELDS}, "dispenser": cfg.get("dispenser", True), "private": bool(cfg.get("private")), "group": cfg.get("group") if cfg.get("group") in provision.GROUPS else "Generic", "modified_at": cfg.get("modified_at", 0)}
        code = normalize_code(cfg.get("pool_code"))
        if not code:
            applog.log.warning(f"Pool {config_pool_name(cfg)!r} has no pool code; add one in configs.json to make it claimable.")
            normalized.setdefault(f"?{config_pool_name(cfg)}", cfg)
            continue
        if code in used:
            old = code
            code = generate_pool_code(used)
            applog.log.warning(f"Duplicate pool code {old} regenerated as {code}.")
        cfg["pool_code"] = code
        used.add(code)
        normalized[code] = cfg
    return normalized


def migrate_pool_entries(entries, configs):
    by_name = {cfg.get("pool_name"): ref for ref, cfg in configs.items() if cfg.get("pool_name") and not ref.startswith("?")}
    for entry in entries:
        ref = by_name.get(entry.get("pool"))
        if ref:
            entry["pool_code"] = ref
            del entry["pool"]
    return entries


def ensure_uids(entries):
    for entry in entries:
        if "uid" not in entry:
            entry["uid"] = uuid.uuid4().hex
    return entries


def startup():
    configs = poolstore.update(CONFIGS_FILE, normalize_configs, dict)
    poolstore.update(POOL_FILE, lambda entries: ensure_uids(migrate_pool_entries([{k: v for k, v in e.items() if k not in ("reserved", "destroying")} for e in entries], configs)))
    applog.log.info(f"Loaded {len(poolstore.load(POOL_FILE))} VM(s) from {POOL_FILE}")
    if REAP_INTERVAL_SECONDS > 0:
        threading.Thread(target=reap_expired_vms, daemon=True).start()


startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
