import time
import json
import hmac
import hashlib
import base64
import uuid
import concurrent.futures
import socket
import requests
import os
from pathlib import Path

import applog
import poolstore

from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding as crypto_padding

load_dotenv()

INT_FIELDS = {"template_vm_id", "guac_link_ttl_seconds", "vm_count"}

SECRET_FIELDS = {"proxmox_token_secret", "template_vm_password", "guacamole_key"}

VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() in ("true", "1", "yes")


def default_config():
    url_output_file = os.getenv("URL_OUTPUT_FILE", "pool.json")
    if not os.path.isabs(url_output_file):
        url_output_file = str(Path(__file__).parent / url_output_file)
    return {
        "proxmox_url": os.getenv("PROXMOX_URL"),
        "proxmox_user": os.getenv("PROXMOX_USER"),
        "proxmox_token_name": os.getenv("PROXMOX_TOKEN_NAME"),
        "proxmox_token_secret": os.getenv("PROXMOX_TOKEN_SECRET"),
        "proxmox_node": os.getenv("PROXMOX_NODE"),
        "template_vm_access_method": os.getenv("TEMPLATE_VM_ACCESS_METHOD"),
        "template_vm_id": os.getenv("TEMPLATE_VM_ID"),
        "template_vm_username": os.getenv("TEMPLATE_VM_USERNAME"),
        "template_vm_password": os.getenv("TEMPLATE_VM_PASSWORD"),
        "guacamole_url": os.getenv("GUACAMOLE_URL"),
        "guacamole_internal_url": os.getenv("GUACAMOLE_INTERNAL_URL") or os.getenv("GUACAMOLE_URL"),
        "guacamole_key": os.getenv("GUACAMOLE_KEY"),
        "guac_link_ttl_seconds": os.getenv("GUAC_LINK_TTL_SECONDS", 7200),
        "url_output_file": url_output_file,
        "vm_count": os.getenv("VM_COUNT", 5),
        "pool_name": os.getenv("POOL_NAME", ""),
        "pool_code": os.getenv("POOL_CODE", ""),
        "dispenser": "",
    }


def build_config(overrides=None):
    config = default_config()
    if overrides:
        for key, value in overrides.items():
            if key in config and value is not None and value != "":
                config[key] = value

    # Mint tokens against the internal/direct Guacamole address (fast, no
    # Cloudflare hairpin / shared rate-limit bucket); students still get the
    # public URL. Falls back to the public URL when no internal one is set.
    if not config.get("guacamole_internal_url"):
        config["guacamole_internal_url"] = config["guacamole_url"]

    for field in INT_FIELDS:
        config[field] = int(config[field])

    proxmox_host = config["proxmox_url"]
    proxmox_scheme = "https"
    if proxmox_host.startswith("http://"):
        proxmox_scheme = "http"
        proxmox_host = proxmox_host[len("http://"):]
    elif proxmox_host.startswith("https://"):
        proxmox_host = proxmox_host[len("https://"):]
    config["proxmox_host"] = proxmox_host
    config["proxmox_scheme"] = proxmox_scheme

    return config


def get_proxmox_client(config):
    proxmox = ProxmoxAPI(
        config["proxmox_host"],
        user=config["proxmox_user"],
        token_name=config["proxmox_token_name"],
        token_value=config["proxmox_token_secret"],
        verify_ssl=VERIFY_SSL
    )
    if config["proxmox_scheme"] == "http":
        proxmox._store["base_url"] = proxmox._store["base_url"].replace("https://", "http://", 1)
    return proxmox


def get_port(access_method):
    if access_method == "ssh":
        return "22"
    elif access_method == "vnc":
        return "5900"
    elif access_method == "rdp":
        return "3389"
    else:
        raise ValueError(f"Unrecognized TEMPLATE_VM_ACCESS_METHOD: {access_method!r}")


def generate_guac_url(config, target_ip, student_id):
    secret_key = bytes.fromhex(config["guacamole_key"])
    expires_at = time.time() + config["guac_link_ttl_seconds"]
    access_method = config["template_vm_access_method"]

    payload = {
        "username": student_id,
        "expires": int(expires_at * 1000),
        "connections": {
            f"Workshop VM - {student_id}": {
                "id": str(uuid.uuid4()),
                "protocol": access_method,
                "parameters": {
                    "hostname": target_ip,
                    "port": get_port(access_method),
                    "username": config["template_vm_username"],
                    "password": config["template_vm_password"],
                    "ignore-cert": "true",
                    "security": "nla"
                }
            }
        }
    }

    json_data = json.dumps(payload, separators=(',', ':'))
    signature = hmac.new(secret_key, json_data.encode('utf-8'), hashlib.sha256).digest()
    signed_data = signature + json_data.encode('utf-8')

    # zero IV is what Guacamole's client-encryption spec mandates
    iv = b'\x00' * 16
    cipher = Cipher(algorithms.AES(secret_key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()

    padder = crypto_padding.PKCS7(algorithms.AES.block_size).padder()
    padded_data = padder.update(signed_data) + padder.finalize()
    encrypted_data = encryptor.update(padded_data) + encryptor.finalize()

    base64_encrypted = base64.b64encode(encrypted_data).decode('utf-8')

    api_url = config.get("guacamole_internal_url") or config["guacamole_url"]
    response = requests.post(
        f"{api_url}/api/tokens",
        data={"data": base64_encrypted},
        timeout=15
    )

    if response.status_code == 200:
        return f"{config['guacamole_url']}/?token={response.json().get('authToken')}", expires_at
    raise RuntimeError(f"Guacamole token request failed: {response.status_code} {response.text}")


def get_vm_ip(proxmox, config, vmid, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            interfaces = proxmox.nodes(config["proxmox_node"]).qemu(vmid).agent.get("network-get-interfaces")
            for interface in interfaces.get('result', []):
                if interface['name'] in ['lo', 'docker0']:
                    continue

                for ip_info in interface.get('ip-addresses', []):
                    if ip_info['ip-address-type'] == 'ipv4':
                        ip = ip_info['ip-address']

                        if ip.startswith("127.") or ip.startswith("169.254") or ip.startswith("172.17"):
                            continue

                        return ip
        except Exception:
            pass
        time.sleep(3)
    raise TimeoutError(f"VM {vmid} did not obtain a valid IP within {timeout} seconds")


def wait_for_port(ip, port):
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            with socket.create_connection((ip, port), timeout=2):
                time.sleep(2)
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(3)
    raise TimeoutError(f"Port {port} on {ip} did not open within 120 seconds")


def provision_worker(proxmox, config, vmid, student_id, log):
    node = proxmox.nodes(config["proxmox_node"])
    access_method = config["template_vm_access_method"]

    try:
        log(f"[{vmid}] Cloning template...")
        node.qemu(config["template_vm_id"]).clone.post(newid=vmid, name=f"workshop-{student_id}", full=0)

        log(f"[{vmid}] Booting VM...")
        node.qemu(vmid).status.start.post()

        log(f"[{vmid}] Waiting for IP...")
        vm_ip = get_vm_ip(proxmox, config, vmid)

        log(f"[{vmid}] Waiting for {access_method}...")
        wait_for_port(vm_ip, int(get_port(access_method)))

        guac_url, expires_at = generate_guac_url(config, vm_ip, student_id)
        return vmid, student_id, guac_url, expires_at
    except Exception as exc:
        log(f"[{vmid}] Provision failed: {exc}. Cleaning up VM...")
        try:
            status = node.qemu(vmid).status.current.get()
            if status.get("status") == "running":
                node.qemu(vmid).status.stop.post()
        except Exception:
            pass
        try:
            node.qemu(vmid).delete()
        except Exception:
            pass
        raise


def append_pool_entries(output_file, entries):
    return poolstore.update(output_file, lambda pool: pool + entries)


def provision_one(config, student_id, log=applog.log.info):
    proxmox = get_proxmox_client(config)
    vmid = int(proxmox.cluster.nextid.get())
    return provision_worker(proxmox, config, vmid, student_id, log)


def run_parallel_provisioning(config, count=None, log=applog.log.info):
    proxmox = get_proxmox_client(config)
    count = count if count is not None else config["vm_count"]

    log(f"\n--- Pre-allocating {count} VMIDs ---")
    used_vmids = {vm["vmid"] for vm in proxmox.nodes(config["proxmox_node"]).qemu.get()}
    tasks = []
    candidate_vmid = int(proxmox.cluster.nextid.get())

    for i in range(count):
        while candidate_vmid in used_vmids:
            candidate_vmid += 1
        target_vmid = candidate_vmid
        used_vmids.add(target_vmid)
        candidate_vmid += 1

        student_id = f"student-{i+1}"
        tasks.append((target_vmid, student_id))
        log(f"Allocated {target_vmid} to {student_id}")

    log(f"\n--- Firing off Proxmox Clones in Parallel ---")
    results = []

    # keep max_workers low or parallel clones hammer the Proxmox API
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(provision_worker, proxmox, config, vmid, sid, log): sid
            for vmid, sid in tasks
        }

        for future in concurrent.futures.as_completed(futures):
            try:
                vmid, student_id, url, expires_at = future.result()
                results.append((vmid, student_id, url, expires_at))
                log(f"✅ {student_id} is ready! (token URL stored)")
            except Exception as exc:
                log(f"❌ VM creation failed: {exc}")

    if results:
        log("\n=== ALL WORKSHOP VMS PROVISIONED ===")
        results.sort(key=lambda x: int(x[1].split('-')[1]))
        for _, student, url, _ in results:
            log(f"{student}) {url}")
    else:
        log("\n=== NO WORKSHOP VMS WERE PROVISIONED ===")

    pool_output_file = config["url_output_file"]
    access_method = config["template_vm_access_method"]
    new_entries = [
        {"vmid": v, "student_id": s, "url": u, "claimed": False, "expires_at": e,
         "access_method": access_method, "pool": config["pool_name"],
         "template_vm_username": config["template_vm_username"],
         "template_vm_password": config["template_vm_password"]}
        for v, s, u, e in results
    ]
    full_pool = append_pool_entries(pool_output_file, new_entries)
    log(f"\nAdded {len(new_entries)} VMs to pool (now {len(full_pool)} total)")

    return new_entries


if __name__ == "__main__":
    cli_config = build_config()
    applog.log.info(f"=== Creating {cli_config['vm_count']} workshop VMs ===")
    run_parallel_provisioning(cli_config)

RECONNECT_IP_TIMEOUT = 30


def mint_session_url(entry, log=applog.log.info):
    """Mint a fresh session URL for an existing entry, using its current IP and
    its stored access credentials (legacy entries fall back to .env defaults)."""
    config = build_config({
        "template_vm_access_method": entry.get("access_method"),
        "template_vm_username": entry.get("template_vm_username"),
        "template_vm_password": entry.get("template_vm_password"),
    })
    proxmox = get_proxmox_client(config)
    vmid = entry["vmid"]
    try:
        status = proxmox.nodes(config["proxmox_node"]).qemu(vmid).status.current.get()
    except Exception as exc:
        raise RuntimeError(f"VM {vmid} not found on Proxmox: {exc}")
    if status.get("status") != "running":
        raise RuntimeError(f"VM {vmid} is not running (status={status.get('status')})")
    vm_ip = get_vm_ip(proxmox, config, vmid, timeout=RECONNECT_IP_TIMEOUT)
    url, _ = generate_guac_url(config, vm_ip, entry.get("student_id") or f"vm-{vmid}")
    log(f"[{vmid}] Minted fresh session URL (ip={vm_ip})")
    return url
