import fcntl
import time
import json
import concurrent.futures
import os
from pathlib import Path
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI

load_dotenv()

# Safety net: only VMs with this name prefix are ever touched
WORKSHOP_PREFIX = "workshop-"

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
        "url_output_file": url_output_file,
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
        verify_ssl=VERIFY_SSL
    )
    if config["proxmox_scheme"] == "http":
        proxmox._store["base_url"] = proxmox._store["base_url"].replace("https://", "http://", 1)
    return proxmox


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


def run_teardown(config, mode="all", vmids=None, log=print):
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
        current_pool = load_pool(pool_output_file)
        remaining_pool = [entry for entry in current_pool if entry.get('vmid') not in destroyed_vmids]
        tmp = pool_output_file + ".tmp"
        with open(tmp, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(remaining_pool, f, indent=2)
                f.flush()
                os.fchmod(f.fileno(), 0o600)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, pool_output_file)
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
