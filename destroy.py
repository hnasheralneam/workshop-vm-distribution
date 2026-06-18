import time
import json
import concurrent.futures
import os
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI

# ==========================================
# CONFIGURATION
# ==========================================
load_dotenv()

POOL_OUTPUT_FILE = os.getenv("URL_OUTPUT_FILE")

PROXMOX_HOST = os.getenv("PROXMOX_URL")
if PROXMOX_HOST.startswith("http://"):
    PROXMOX_HOST = PROXMOX_HOST[len("http://"):]
elif PROXMOX_HOST.startswith("https://"):
    PROXMOX_HOST = PROXMOX_HOST[len("https://"):]
PROXMOX_USER = os.getenv("PROXMOX_USER")
PROXMOX_TOKEN_NAME = os.getenv("PROXMOX_TOKEN_NAME")
PROXMOX_TOKEN_SECRET = os.getenv("PROXMOX_TOKEN_SECRET")
PROXMOX_NODE = os.getenv("PROXMOX_NODE")

# The critical safety net: Only VMs starting with this prefix will be touched
WORKSHOP_PREFIX = "workshop-"
# ==========================================

proxmox = ProxmoxAPI(
    PROXMOX_HOST,
    user=PROXMOX_USER,
    token_name=PROXMOX_TOKEN_NAME,
    token_value=PROXMOX_TOKEN_SECRET,
    verify_ssl=False
)

def destroy_worker(vmid, vm_name):
    """Worker task to safely stop and destroy a single VM."""
    node = proxmox.nodes(PROXMOX_NODE)

    try:
        # 1. Check current status
        current_status = node.qemu(vmid).status.current.get()

        # 2. Stop the VM if it is running (Proxmox will not delete a running VM)
        if current_status.get("status") == "running":
            print(f"[{vmid}] 🛑 Stopping {vm_name}...")
            node.qemu(vmid).status.stop.post()

            # Poll until the VM is actually stopped
            while True:
                time.sleep(2)
                status = node.qemu(vmid).status.current.get().get("status")
                if status == "stopped":
                    break

        # 3. Destroy the VM
        print(f"[{vmid}] 💥 Destroying {vm_name}...")
        node.qemu(vmid).delete()
        return f"✅ Successfully destroyed {vm_name} ({vmid})"

    except Exception as e:
        return f"❌ Failed to destroy {vm_name} ({vmid}): {e}"

def run_teardown():
    print(f"\n--- Scanning for VMs with prefix '{WORKSHOP_PREFIX}' ---")

    # Fetch all VMs on the node
    all_vms = proxmox.nodes(PROXMOX_NODE).qemu.get()

    # Filter for workshop VMs
    target_vms = [vm for vm in all_vms if vm.get('name', '').startswith(WORKSHOP_PREFIX)]

    if not target_vms:
        print("No workshop VMs found. Your cluster is clean!")
        return

    print(f"Found {len(target_vms)} workshop VMs to destroy.")

    # Execute the destruction in parallel
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(destroy_worker, vm['vmid'], vm['name']): vm for vm in target_vms}

        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    print("\n=== TEARDOWN COMPLETE ===")
    for result in results:
        print(result)

    if POOL_OUTPUT_FILE:
        with open(POOL_OUTPUT_FILE, "w") as f:
            json.dump([], f, indent=2)
        print(f"\nCleared pool file: {POOL_OUTPUT_FILE}")

if __name__ == "__main__":
    # Add a safety prompt to prevent accidental execution
    confirm = input("⚠️ WARNING: This will immediately power off and destroy all workshop VMs. Type 'yes' to proceed: ")
    if confirm.strip().lower() == 'yes':
        run_teardown()
    else:
        print("Teardown aborted.")
