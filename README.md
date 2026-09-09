# Workshop VM Distribution

Provisions per-student workshop VMs on Proxmox and hands them out through a web portal, using Guacamole for browser-based terminal access.  
Great for students to follow along with a hands-on box during a workshop, or to give take-home practice vms.

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
- `GUACAMOLE_URL`, `GUACAMOLE_INTERNAL_URL`, `GUACAMOLE_KEY`
- `URL_OUTPUT_FILE`, `ADMIN_PASSWORD`, `LOG_FILE`

Template VM id/username/password, access method, VM count, and link duration aren't in `.env` — they're entered per pool, either in the admin "New deployment" form or interactively when running `provision.py` directly.

## CLI Usage
1. Provision VMs (writes `pool.json`; prompts for access method, template VM id/username/password, VM count, and link TTL):
   ```bash
   python provision.py
   ```
2. Start the portal (serves `index.html` on `http://0.0.0.0:5000`, students claim a VM at `/api/claim`):
   ```bash
   gunicorn -w 1 --threads "$(( $(nproc) > 2 ? $(nproc) - 1 : $(nproc) ))" -b 0.0.0.0:5000 server:app
   ```
   `-w 1` is required: pool/job state and the VM-reaper thread live in process memory, so extra worker processes would each keep their own out-of-sync copy. `--threads` gives concurrency within that one process instead (cores − 1 above 2 cores, otherwise all cores, so this also works on single- and dual-core systems). (`python server.py` still works for quick local testing, but runs Flask's unhardened dev server.)
   To provision a whole fresh host instead, `sudo bash deploy/setup.sh` (flags + env overrides in the script header) installs the Guacamole docker stack, clones + sets up the app, writes `.env`, and installs `deploy/workshop-vm.service` — a `workshop-vm` systemd service whose `ExecStart` is `deploy/run.sh`, the gunicorn command above resolving paths from its own location. Run the portal by hand with `deploy/run.sh`.
3. Tear down all workshop VMs when done (interactive confirmation, only removes `workshop-` prefixed VMs):
   ```bash
   python destroy.py
   ```

## Student portal
`http://0.0.0.0:5000` shows one button per saved pool (plus any legacy pool.json entries); picking one claims a random free VM from that pool and redirects to its Guacamole session — and when the pool has none free, a fresh VM is provisioned on the spot while the page shows progress, then redirects. Codes don't gate anything here: a pool with a code is listed and claimable exactly like any other. The "Enter pool code" button is just an alternate way to claim — type a pool's code and you get a machine from that pool without picking it by name. The page remembers the assigned machine, so Reconnect re-opens it with a fresh session token, and an expired machine is swapped for a free replacement on the next visit.

## Admin portal
`http://0.0.0.0:5000/admin` provides a web UI for provisioning and destroying VMs without touching the CLI — set `ADMIN_PASSWORD` in `.env` to protect it (HTTP Basic Auth). From there you can kick off a provisioning run with overridden settings (template, access method, VM count, VM duration, etc.), destroy all/expired/selected VMs, and watch job progress and the live pool table. Each run is saved as a named pool (in `configs.json`) that you can redeploy with one click, reconfigure, or delete from the deploy dialog. Every pool also gets a 5-character claim code (auto-generated, editable in the same dialog) that students can enter on the portal to claim from that pool — codes are a claim shortcut, not a lock, so coded pools still appear in the public pool list and their `/claim/<pool>` links work with or without the code — opening one claims a VM from that pool immediately, and anyone who already holds a machine is warned that claiming a new one deletes their current one right away. The deploy dialog also has a Dispenser checkbox: a dispenser pool provisions a fresh VM whenever someone loads its claim link and the pool has none free (uncapped). Pools saved before this option existed are migrated to dispensers on the next server start; uncheck the box in the edit dialog to keep a pool claim-only.

## Notes
Make sure you don't have any important vms named workshop-* in your proxmox! `destroy.py` will delete them.
State lives in `pool.json` (claims + per-VM credentials) and `configs.json` (saved pools); both are gitignored and written with `0600` permissions. Server, provisioning, and teardown output also goes to `server.log` (gitignored, `0600`, rotates at 5 MB with 2 backups; set `LOG_FILE` to change the location) while still printing to the console.