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

from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding as crypto_padding

# ==========================================
# CONFIGURATION
# ==========================================

load_dotenv()

# Fields that must be cast to int once overrides are merged in.
INT_FIELDS = {"template_vm_id", "guac_link_ttl_seconds", "vm_count"}

# Fields considered sensitive; never echoed back to a UI.
SECRET_FIELDS = {"proxmox_token_secret", "template_vm_password", "guacamole_key"}


def default_config():
    """Config derived from the environment. Used by the CLI and as a base for overrides."""
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
        "guacamole_key": os.getenv("GUACAMOLE_KEY"),
        "guac_link_ttl_seconds": os.getenv("GUAC_LINK_TTL_SECONDS", 7200),
        "url_output_file": os.getenv("URL_OUTPUT_FILE"),
        "vm_count": os.getenv("VM_COUNT", 5),
    }


def build_config(overrides=None):
    """Merge overrides on top of the env defaults and normalize derived fields."""
    config = default_config()
    if overrides:
        for key, value in overrides.items():
            if key in config and value is not None and value != "":
                config[key] = value

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
        verify_ssl=False
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
    """Encrypts the payload and fetches the Guacamole token URL."""
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

    iv = b'\x00' * 16
    cipher = Cipher(algorithms.AES(secret_key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()

    padder = crypto_padding.PKCS7(algorithms.AES.block_size).padder()
    padded_data = padder.update(signed_data) + padder.finalize()
    encrypted_data = encryptor.update(padded_data) + encryptor.finalize()

    base64_encrypted = base64.b64encode(encrypted_data).decode('utf-8')

    response = requests.post(
        f"{config['guacamole_url']}/api/tokens",
        data={"data": base64_encrypted}
    )

    if response.status_code == 200:
        return f"{config['guacamole_url']}/?token={response.json().get('authToken')}", expires_at
    return "Error generating Guacamole URL", expires_at


def get_vm_ip(proxmox, config, vmid):
    """Polls the guest-agent until a VALID, routable IPv4 address is found."""
    while True:
        try:
            interfaces = proxmox.nodes(config["proxmox_node"]).qemu(vmid).agent.get("network-get-interfaces")
            for interface in interfaces.get('result', []):
                # Ignore loopback and common virtual interfaces
                if interface['name'] in ['lo', 'docker0']:
                    continue

                for ip_info in interface.get('ip-addresses', []):
                    if ip_info['ip-address-type'] == 'ipv4':
                        ip = ip_info['ip-address']

                        # Ignore APIPA (DHCP failure) and Docker default subnets
                        if ip.startswith("127.") or ip.startswith("169.254") or ip.startswith("172.17"):
                            continue

                        # We found a real IP!
                        return ip
        except Exception:
            pass
        time.sleep(3)


def wait_for_port(ip, port):
    # Attempts a TCP connection to the port until the remote access daemon answers
    while True:
        try:
            # Attempt to open a socket to the port
            with socket.create_connection((ip, port), timeout=2):
                # Once it connects, give the service an 2 extra seconds to fully bind/load keys
                time.sleep(2)
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            # Port is closed or unreachable, wait and try again
            time.sleep(3)


def provision_worker(proxmox, config, vmid, student_id, log):
    """The task that each thread will execute independently."""
    node = proxmox.nodes(config["proxmox_node"])
    access_method = config["template_vm_access_method"]

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


def run_parallel_provisioning(config, count=None, log=print):
    proxmox = get_proxmox_client(config)
    count = count if count is not None else config["vm_count"]

    # 1. Pre-allocate all VMIDs safely on the main thread, skipping any IDs already in use
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

    # 2. Fire off all the clones at the exact same time
    log(f"\n--- Firing off Proxmox Clones in Parallel ---")
    results = []

    # max_workers dictates how many VMs build at once.
    # Keep it under 20 so you don't DDoS your own Proxmox API.
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Submit tasks to the pool
        futures = {
            executor.submit(provision_worker, proxmox, config, vmid, sid, log): sid
            for vmid, sid in tasks
        }

        # Gather results as they finish
        for future in concurrent.futures.as_completed(futures):
            try:
                vmid, student_id, url, expires_at = future.result()
                results.append((vmid, student_id, url, expires_at))
                log(f"✅ {student_id} is ready!")
            except Exception as exc:
                log(f"❌ VM creation failed: {exc}")

    # 3. Print the final list cleanly
    log("\n=== ALL WORKSHOP VMS PROVISIONED ===")
    # Sort them so student-1 is at the top
    results.sort(key=lambda x: int(x[1].split('-')[1]))
    for _, student, url, _ in results:
        log(f"{student}) {url}")

    pool_output_file = config["url_output_file"]
    existing_pool = []
    if os.path.exists(pool_output_file):
        with open(pool_output_file) as f:
            existing_pool = json.load(f)

    access_method = config["template_vm_access_method"]
    new_entries = [
        {"vmid": v, "student_id": s, "url": u, "claimed": False, "expires_at": e, "access_method": access_method}
        for v, s, u, e in results
    ]
    full_pool = existing_pool + new_entries

    with open(pool_output_file, "w") as f:
        json.dump(full_pool, f, indent=2)
    log(f"\nAdded {len(new_entries)} VMs to pool (now {len(full_pool)} total)")

    return new_entries


if __name__ == "__main__":
    cli_config = build_config()
    print(f"=== Creating {cli_config['vm_count']} workshop VMs ===")
    run_parallel_provisioning(cli_config)
