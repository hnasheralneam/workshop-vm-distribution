import fcntl
import time
import json
import concurrent.futures
import os

import applog
import poolstore
from provision import build_config, get_proxmox_client
from dotenv import load_dotenv

load_dotenv()

# Safety net: only VMs with this name prefix are ever touched
WORKSHOP_PREFIX = "workshop-"


def destroy_worker(proxmox, node_name, vmid, vm_name, log):
    node = proxmox.nodes(node_name)

    try:
        current_status = node.qemu(vmid).status.current.get()

        # Proxmox won't delete a running VM
        if current_status.get("status") == "running":
            log(f"[{vmid}] 🛑 Stopping {vm_name}...")
            node.qemu(vmid).status.stop.post()

            deadline = time.time() + 120
            while time.time() < deadline:
                time.sleep(2)
                status = node.qemu(vmid).status.current.get().get("status")
                if status == "stopped":
                    break
            else:
                raise TimeoutError(f"VM {vmid} did not stop within 120 seconds")

        log(f"[{vmid}] 💥 Destroying {vm_name}...")
        node.qemu(vmid).delete()
        return f"✅ Successfully destroyed {vm_name} ({vmid})"

    except Exception as e:
        raise RuntimeError(f"❌ Failed to destroy {vm_name} ({vmid}): {e}") from e


def load_pool(pool_output_file):
    if pool_output_file and os.path.exists(pool_output_file):
        with open(pool_output_file, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return []


def run_teardown(config, mode="all", vmids=None, log=applog.log):
    """mode: 'all', 'expired', or 'specific' (vmids required for 'specific')."""
    proxmox = get_proxmox_client(config)
    pool_output_file = config["url_output_file"]

    log(f"\n--- Scanning for VMs with prefix '{WORKSHOP_PREFIX}' ---")

    all_vms = proxmox.nodes(config["proxmox_node"]).qemu.get()
    target_vms = [vm for vm in all_vms if vm.get('name', '').startswith(WORKSHOP_PREFIX)]

    pool = load_pool(pool_output_file)

    if mode == "specific":
        wanted = {int(v) for v in (vmids or [])}
        target_vms = [vm for vm in target_vms if vm.get('vmid') in wanted]
    elif mode == "expired":
        now = time.time()
        expired_vmids = {entry['vmid'] for entry in pool if entry.get('expires_at') is not None and entry['expires_at'] < now}
        target_vms = [vm for vm in target_vms if vm.get('vmid') in expired_vmids]
    else:
        pass

    if not target_vms:
        log("No matching workshop VMs found. Nothing to destroy!")
        return []

    log(f"Found {len(target_vms)} workshop VMs to destroy.")

    results = []
    destroyed_vmids = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(destroy_worker, proxmox, config["proxmox_node"], vm['vmid'], vm['name'], log): vm
            for vm in target_vms
        }

        for future in concurrent.futures.as_completed(futures):
            vm = futures[future]
            try:
                results.append(future.result())
                destroyed_vmids.add(vm['vmid'])
            except Exception as exc:
                results.append(str(exc))

    log("\n=== TEARDOWN COMPLETE ===")
    for result in results:
        log(result)

    if pool_output_file:
        remaining_pool = poolstore.update(
            pool_output_file,
            lambda entries: [entry for entry in entries if entry.get('vmid') not in destroyed_vmids])
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
            applog.log.info("Teardown aborted.")
    elif choice == 'e':
        confirm = input("⚠️ WARNING: This will immediately power off and destroy EXPIRED workshop VMs. Type 'yes' to proceed: ")
        if confirm.strip().lower() == 'yes':
            run_teardown(cli_config, mode="expired")
        else:
            applog.log.info("Teardown aborted.")
    else:
        applog.log.info("Teardown aborted.")
