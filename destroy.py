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

# The critical safety net: Only VMs starting with this prefix will be touched
WORKSHOP_PREFIX = "workshop-"


def default_config():
    return {
        "proxmox_url": os.getenv("PROXMOX_URL"),
        "proxmox_user": os.getenv("PROXMOX_USER"),
        "proxmox_token_name": os.getenv("PROXMOX_TOKEN_NAME"),
        "proxmox_token_secret": os.getenv("PROXMOX_TOKEN_SECRET"),
        "proxmox_node": os.getenv("PROXMOX_NODE"),
        "url_output_file": os.getenv("URL_OUTPUT_FILE"),
    }


def build_config(overrides=None):
    config = default_config()
    if overrides:
        for key, value in overrides.items():
            if key in config and value is not None and value != "":
                config[key] = value

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


def destroy_worker(proxmox, node_name, vmid, vm_name, log):
    """Worker task to safely stop and destroy a single VM."""
    node = proxmox.nodes(node_name)

    try:
        # 1. Check current status
        current_status = node.qemu(vmid).status.current.get()

        # 2. Stop the VM if it is running (Proxmox will not delete a running VM)
        if current_status.get("status") == "running":
            log(f"[{vmid}] 🛑 Stopping {vm_name}...")
            node.qemu(vmid).status.stop.post()

            # Poll until the VM is actually stopped
            while True:
                time.sleep(2)
                status = node.qemu(vmid).status.current.get().get("status")
                if status == "stopped":
                    break

        # 3. Destroy the VM
        log(f"[{vmid}] 💥 Destroying {vm_name}...")
        node.qemu(vmid).delete()
        return f"✅ Successfully destroyed {vm_name} ({vmid})"

    except Exception as e:
        return f"❌ Failed to destroy {vm_name} ({vmid}): {e}"


def load_pool(pool_output_file):
    if pool_output_file and os.path.exists(pool_output_file):
        with open(pool_output_file) as f:
            return json.load(f)
    return []


def run_teardown(config, mode="all", vmids=None, log=print):
    """mode: 'all', 'expired', or 'specific' (requires vmids, a collection of ints)."""
    proxmox = get_proxmox_client(config)
    pool_output_file = config["url_output_file"]

    log(f"\n--- Scanning for VMs with prefix '{WORKSHOP_PREFIX}' ---")

    # Fetch all VMs on the node
    all_vms = proxmox.nodes(config["proxmox_node"]).qemu.get()

    # Filter for workshop VMs - the critical safety net
    target_vms = [vm for vm in all_vms if vm.get('name', '').startswith(WORKSHOP_PREFIX)]

    pool = load_pool(pool_output_file)
    remaining_pool = pool
    removed_vmids = set()

    if mode == "specific":
        wanted = {int(v) for v in (vmids or [])}
        target_vms = [vm for vm in target_vms if vm.get('vmid') in wanted]
        removed_vmids = {vm['vmid'] for vm in target_vms}
        remaining_pool = [entry for entry in pool if entry['vmid'] not in removed_vmids]
    elif mode == "expired":
        now = time.time()
        removed_vmids = {entry['vmid'] for entry in pool if entry.get('expires_at', 0) < now}
        remaining_pool = [entry for entry in pool if entry['vmid'] not in removed_vmids]
        target_vms = [vm for vm in target_vms if vm.get('vmid') in removed_vmids]
    else:  # all
        removed_vmids = {vm['vmid'] for vm in target_vms}
        remaining_pool = []

    if not target_vms:
        log("No matching workshop VMs found. Nothing to destroy!")
        return []

    log(f"Found {len(target_vms)} workshop VMs to destroy.")

    # Execute the destruction in parallel
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(destroy_worker, proxmox, config["proxmox_node"], vm['vmid'], vm['name'], log): vm
            for vm in target_vms
        }

        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    log("\n=== TEARDOWN COMPLETE ===")
    for result in results:
        log(result)

    if pool_output_file:
        with open(pool_output_file, "w") as f:
            json.dump(remaining_pool, f, indent=2)
        log(f"\nUpdated pool file: {pool_output_file} ({len(remaining_pool)} entries remaining)")

    return results


if __name__ == "__main__":
    cli_config = build_config()
    choice = input("Destroy ALL workshop VMs (A), only EXPIRED VMs (E), or anything else to quit: ").strip().lower()

    if choice == 'a':
        confirm = input("⚠️ WARNING: This will immediately power off and destroy ALL workshop VMs. Type 'yes' to proceed: ")
        if confirm.strip().lower() == 'yes':
            run_teardown(cli_config, mode="all")
        else:
            print("Teardown aborted.")
    elif choice == 'e':
        confirm = input("⚠️ WARNING: This will immediately power off and destroy EXPIRED workshop VMs. Type 'yes' to proceed: ")
        if confirm.strip().lower() == 'yes':
            run_teardown(cli_config, mode="expired")
        else:
            print("Teardown aborted.")
    else:
        print("Teardown aborted.")
