import time
import json
import hmac
import hashlib
import base64
import urllib.parse
import uuid
import concurrent.futures
import socket
import requests
import os

from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from Crypto.Hash import HMAC, SHA256
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding as crypto_padding

# ==========================================
# CONFIGURATION
# ==========================================

load_dotenv()

proxmox_host = os.getenv("PROXMOX_URL")
proxmox_scheme = "https"
if proxmox_host.startswith("http://"):
    proxmox_scheme = "http"
    proxmox_host = proxmox_host[len("http://"):]
elif proxmox_host.startswith("https://"):
    proxmox_host = proxmox_host[len("https://"):]
proxmox_user = os.getenv("PROXMOX_USER")
proxmox_token_name = os.getenv("PROXMOX_TOKEN_NAME")
proxmox_token_secret = os.getenv("PROXMOX_TOKEN_SECRET")
proxmox_node = os.getenv("PROXMOX_NODE")

access_method = os.getenv("TEMPLATE_VM_ACCESS_METHOD")
template_vm_id = os.getenv("TEMPLATE_VM_ID")
vm_username = os.getenv("TEMPLATE_VM_USERNAME")
vm_password = os.getenv("TEMPLATE_VM_PASSWORD")

guacamole_url = os.getenv("GUACAMOLE_URL")
guacamole_key = os.getenv("GUACAMOLE_KEY")
guac_link_ttl_seconds = int(os.getenv("GUAC_LINK_TTL_SECONDS", 7200))

pool_output_file = os.getenv("URL_OUTPUT_FILE")
vm_count = int(os.getenv("VM_COUNT", 5))

# ==========================================

proxmox = ProxmoxAPI(
    proxmox_host,
    user=proxmox_user,
    token_name=proxmox_token_name,
    token_value=proxmox_token_secret,
    verify_ssl=False
)

if proxmox_scheme == "http":
    proxmox._store["base_url"] = proxmox._store["base_url"].replace("https://", "http://", 1)

def generate_guac_url(target_ip, student_id):
    """Encrypts the payload and fetches the Guacamole token URL."""
    secret_key = bytes.fromhex(guacamole_key)
    expires_at = time.time() + guac_link_ttl_seconds

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
                    "username": vm_username,
                    "password": vm_password,
                    "ignore-cert": "true",
                    "security": "any"
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
        f"{guacamole_url}/api/tokens",
        data={"data": base64_encrypted}
    )

    if response.status_code == 200:
        return f"{guacamole_url}/?token={response.json().get('authToken')}", expires_at
    return "Error generating Guacamole URL", expires_at

def get_port(access_method):
    if access_method == "ssh":
        return "22"
    elif access_method == "vnc":
        return "5900"
    elif access_method == "rdp":
        return "3389"
    else:
        raise ValueError(f"Unrecognized TEMPLATE_VM_ACCESS_METHOD: {access_method!r}")

def get_vm_ip(vmid):
    """Polls the guest-agent until a VALID, routable IPv4 address is found."""
    while True:
        try:
            interfaces = proxmox.nodes(proxmox_node).qemu(vmid).agent.get("network-get-interfaces")
            for interface in interfaces.get('result', []):
                # Ignore loopback and common virtual interfaces
                if interface['name'] in ['lo', 'docker0']:
                    continue

                for ip_info in interface.get('ip-addresses', []):
                    if ip_info['ip-address-type'] == 'ipv4':
                        ip = ip_info['ip-address']

                        # Ignore APIPA (DHCP failure) and Docker default subnets
                        if ip.startswith("169.254") or ip.startswith("172.17"):
                            continue

                        # We found a real IP!
                        return ip
        except Exception:
            pass
        time.sleep(3)

def provision_worker(vmid, student_id):
    """The task that each thread will execute independently."""
    node = proxmox.nodes(proxmox_node)

    print(f"[{vmid}] Cloning template...")
    node.qemu(template_vm_id).clone.post(newid=vmid, name=f"workshop-{student_id}", full=0)

    print(f"[{vmid}] Booting VM...")
    node.qemu(vmid).status.start.post()

    print(f"[{vmid}] Waiting for IP...")
    vm_ip = get_vm_ip(vmid)

    print(f"[{vmid}] Waiting for SSH...")
    wait_for_port(vm_ip, int(get_port(access_method)))

    guac_url, expires_at = generate_guac_url(vm_ip, student_id)
    return vmid, student_id, guac_url, expires_at

def run_parallel_provisioning(count):
    # 1. Pre-allocate all VMIDs safely on the main thread, skipping any IDs already in use
    print(f"\n--- Pre-allocating {count} VMIDs ---")
    used_vmids = {vm["vmid"] for vm in proxmox.nodes(proxmox_node).qemu.get()}
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
        print(f"Allocated {target_vmid} to {student_id}")

    # 2. Fire off all the clones at the exact same time
    print(f"\n--- Firing off Proxmox Clones in Parallel ---")
    results = []

    # max_workers dictates how many VMs build at once.
    # Keep it under 20 so you don't DDoS your own Proxmox API.
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Submit tasks to the pool
        futures = {executor.submit(provision_worker, vmid, sid): sid for vmid, sid in tasks}

        # Gather results as they finish
        for future in concurrent.futures.as_completed(futures):
            try:
                vmid, student_id, url, expires_at = future.result()
                results.append((vmid, student_id, url, expires_at))
                print(f"✅ {student_id} is ready!")
            except Exception as exc:
                print(f"❌ VM creation failed: {exc}")

    # 3. Print the final list cleanly
    print("\n=== ALL WORKSHOP VMS PROVISIONED ===")
    # Sort them so student-1 is at the top
    results.sort(key=lambda x: int(x[1].split('-')[1]))
    for _, student, url, _ in results:
        print(f"{student}) {url}")

    existing_pool = []
    if os.path.exists(pool_output_file):
        with open(pool_output_file) as f:
            existing_pool = json.load(f)

    new_entries = [{"vmid": v, "student_id": s, "url": u, "claimed": False, "expires_at": e} for v, s, u, e in results]
    full_pool = existing_pool + new_entries

    with open(pool_output_file, "w") as f:
        json.dump(full_pool, f, indent=2)
    print(f"\nAdded {len(new_entries)} VMs to pool (now {len(full_pool)} total)")

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


if __name__ == "__main__":
    print(f"=== Creating {vm_count} workshop VMs ===")
    run_parallel_provisioning(vm_count)
