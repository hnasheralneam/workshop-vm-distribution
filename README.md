# Workshop VM Distribution

Provisions per-student workshop VMs on Proxmox and hands them out through a web portal, using Guacamole for browser-based terminal access.

## Requirements
- Python 3
- A Proxmox cluster with an API token
- A Guacamole server
- A template VM (with `qemu-guest-agent` installed) to clone from

## Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Proxmox and Guacamole details:

- `PROXMOX_URL`, `PROXMOX_USER`, `PROXMOX_TOKEN_NAME`, `PROXMOX_TOKEN_SECRET`, `PROXMOX_NODE`, `VERIFY_SSL`
- `TEMPLATE_VM_ACCESS_METHOD` (`ssh`, `vnc`, or `rdp`), `TEMPLATE_VM_ID`, `TEMPLATE_VM_USERNAME`, `TEMPLATE_VM_PASSWORD`
- `GUACAMOLE_URL`, `GUACAMOLE_KEY`, `GUAC_LINK_TTL_SECONDS`, `REAP_INTERVAL_SECONDS`
- `URL_OUTPUT_FILE`, `VM_COUNT`, `ADMIN_PASSWORD`

## CLI Usage
1. Provision VMs (writes `pool.json`):
   ```bash
   python provision.py
   ```
2. Start the portal (serves `index.html` on `http://0.0.0.0:5000`, students claim a VM at `/api/claim`):
   ```bash
   python server.py
   ```
3. Tear down all workshop VMs when done (interactive confirmation, only removes `workshop-` prefixed VMs):
   ```bash
   python destroy.py
   ```

## Student portal
`http://0.0.0.0:5000` shows one button per open pool; picking one claims a random free VM and redirects to its Guacamole session. Coded pools don't appear in the list — students either enter the pool's code (if the pool is empty, a fresh VM is provisioned on the spot) or open its `/claim/<pool>` link. The page remembers the assigned machine, so Reconnect re-opens it with a fresh session token, and an expired machine is swapped for a free replacement on the next visit.

## Admin portal
`http://0.0.0.0:5000/admin` provides a web UI for provisioning and destroying VMs without touching the CLI — set `ADMIN_PASSWORD` in `.env` to protect it (HTTP Basic Auth). From there you can kick off a provisioning run with overridden settings (template, access method, VM count, VM duration, etc.), destroy all/expired/selected VMs, and watch job progress and the live pool table. Each run is saved as a named pool (in `configs.json`) that you can redeploy with one click, reconfigure, or delete from the deploy dialog. Every pool also gets a 5-character claim code (auto-generated, editable in the same dialog): coded pools are hidden from the public pool list and are claimed via the code or the pool's `/claim/<pool>` link — opening it claims a VM from that pool immediately, and anyone who already holds a machine is warned that claiming a new one deletes their current one right away.

## Notes
Make sure you don't have any important vms named workshop-* in your proxmox! `destroy.py` will delete them.
State lives in `pool.json` (claims + per-VM credentials) and `configs.json` (saved pools); both are gitignored and written with `0600` permissions.