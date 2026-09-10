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

Template VM id/username/password, access method, VM count, and link duration aren't in `.env`; they're entered per pool, either in the admin "New deployment" form or interactively when running `provision.py` directly.

## CLI Usage
1. Provision VMs (writes `pool.json`; prompts for access method, template VM id/username/password, VM count, and link TTL):
   ```bash
   python provision.py
   ```
2. Start the portal (serves `index.html` on `http://0.0.0.0:5000`, students claim a VM at `/api/claim`):
   ```bash
   gunicorn -w 1 --threads "$(( $(nproc) > 2 ? $(nproc) - 1 : $(nproc) ))" -b 0.0.0.0:5000 server:app
   ```
   `-w 1` is required: pool/job state and the VM-reaper thread live in process memory, so extra worker processes would each keep their own out-of-sync copy. `--threads` gives concurrency within that one process instead (cores − 1 above 2 cores, otherwise all cores). (`python server.py` still works for quick local testing, but runs Flask's unhardened dev server.)
3. Tear down workshop VMs when done (interactive confirmation, only removes `workshop-`-prefixed VMs that are still tracked in `pool.json`):
   ```bash
   python destroy.py
   ```

## Deployment
You can deploy the whole stack with the deploy script: `sudo bash deploy/setup.sh` (override defaults like `APP_DIR`, `PROXMOX_URL`, or `PORT` via env vars). It installs the Guacamole docker stack, clones and sets up the app, writes `.env`, and installs `deploy/workshop-vm.service`, a `workshop-vm` systemd service that runs the portal via `deploy/run.sh` (the gunicorn command above).

## Student portal
The main page shows one card per saved pool; picking one claims a free VM from that pool and redirects to its Guacamole session. For dispenser pools, when a claim link is loaded and the pool has none free, a fresh VM is provisioned on the spot while the page shows progress, then redirects. The "Enter pool code" button is an alternate way to claim: type a pool's code and you get a machine from that pool without picking it by name. Pools with no free vms that are not dispensers have their claim buttons disabled. The page remembers the assigned machine, so Reconnect re-opens it with a fresh session token, and an expired machine is swapped for a free replacement on the next visit.

## Admin portal
The admin page provides a web UI for provisioning and destroying VMs. You can set the `ADMIN_PASSWORD` in `.env`. On that page you can provision pools with custom configs (template, access method, VM count, VM duration, etc.), destroy all/expired/selected VMs, and watch job progress, as well as see the live pool table. Each run is saved as a named pool (in `configs.json`) that you can redeploy with one click, reconfigure, or delete from the deploy dialog. Every pool also gets a 5-character claim code that students can enter on the portal to claim from that pool. The deploy dialog also has a Dispenser checkbox: a dispenser pool provisions a fresh VM whenever someone loads its claim link and the pool has none free.

## Notes
State lives in `pool.json` (claims + per-VM credentials) and `configs.json` (saved pools); both are gitignored. Server, provisioning, and teardown output also goes to `server.log`; set `LOG_FILE` to change the location.