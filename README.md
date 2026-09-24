# Workshop VM Distribution

Provisions per-student workshop VMs on Proxmox and hands them out through a web portal, using Guacamole for browser-based access.
Great for students to follow along with a hands-on box during a workshop, or to give take-home practice vms with another tool like DawgSec's huitzilopochtli.


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
- `ADMIN_PASSWORD`, `LOG_FILE`, `PORT`

Template VM id/username/password, access method, VM count, and link duration aren't in `.env`; they're entered per pool, either in the admin "New pool" form or interactively when running `provision.py` directly.

## CLI Usage
1. Provision VMs (writes `pool.json`; prompts for access method, template VM id/username/password, VM count, and link TTL):
   ```bash
   python provision.py
   ```
2. Start the portal (serves `index.html` on `http://0.0.0.0:5000`, students claim a VM at `/api/claim`):
   ```bash
   gunicorn -w 1 --threads 32 -b 0.0.0.0:5000 server:app
   ```
   `-w 1` is required: pool/job state and the VM-reaper thread live in process memory, so extra worker processes would each keep their own out-of-sync copy. `--threads` gives concurrency within that one process instead; requests mostly wait on Proxmox and Guacamole, so the thread count isn't tied to cores (`deploy/run.sh` reads `GUNICORN_THREADS`, default 32). (`python server.py` still works for quick local testing, but runs Flask's unhardened dev server.)
3. Tear down workshop VMs when done (interactive confirmation, only removes `workshop-`-prefixed VMs that are still tracked in `pool.json`):
   ```bash
   python destroy.py
   ```

## Deployment
You can deploy the whole stack with the deploy script: `sudo bash deploy/setup.sh`. It prompts for the Proxmox host, user, API token name and secret, node, and the public Guacamole URL; for a non-interactive run export those first (along with overrides like `APP_DIR` or `PORT`), with `PROXMOX_TOKEN_NAME` and `PROXMOX_TOKEN_SECRET` required. It installs the Guacamole docker stack, clones and sets up the app, writes `.env`, and installs `deploy/workshop-vm.service`, a `workshop-vm` systemd service that runs the portal via `deploy/run.sh` (the gunicorn command above). Guacamole runs with encrypted JSON auth only, so it has no login accounts. Stacks deployed before this had a Postgres-backed `guacadmin`/`guacadmin` account; re-running the script removes the `guac_postgres` container, and `docker volume rm guacamole_stack_postgres_data` removes the leftover data.

## Student portal
The main page shows one button per saved pool; picking one claims a free VM from that pool and redirects to its Guacamole session. Claim links (`/claim?code=CODE`) identify a pool by code alone and claim (or provision) immediately on load. When a claim lands on a pool with none free and the pool is a dispenser, a fresh VM is provisioned on the spot while the page shows progress, then redirects. The "Enter code" button is an alternate way to claim: type a pool's code and you get a machine from that pool without picking it by name. Pools with no free VMs that are not dispensers don't show a button. Private pools are hidden from the page entirely and can only be claimed by entering their code. Each browser can hold up to `MAX_VMS` machines at once (default 2, set it in `.env`). The page lists your machines with per-machine Reconnect and Release buttons: Reconnect re-opens one with a fresh session token, Release immediately destroys it and frees the seat, and an expired machine is swapped for a free replacement from the same pool on the next visit. Every portal load silently re-mints a fresh session link for each held machine (new token, current VM IP), so links keep working after the workshop VMs or Guacamole restart; just reopen the portal. Old links opened directly can still idle out, the portal is the way back in.

## Admin portal
The admin page provides a web UI for provisioning and destroying VMs. You can set the `ADMIN_PASSWORD` in `.env`. Saved pools are listed as cards, sorted by most recent use, with a search bar and filters (all, in use, free, dispenser, private). Each card shows the pool name, its claim code with a copy button, a copy button for the student claim link, a lock that toggles the pool between public and private without opening the dialog, live usage counts, and a one-click Deploy with a VM count.

You create pools with the New pool dialog: template, access method, VM count, VM duration, and the Dispenser and Private checkboxes. A dispenser pool provisions a fresh VM whenever someone claims from it and the pool has none free. A private pool is hidden from the main page and can only be claimed with its code. The same dialog renames, reconfigures, or deletes an existing pool. Every pool is assigned a 5-character claim code automatically; the code is the pool's permanent ID, so claim links keep working and pool names can be changed at any time without touching existing VMs.

Below the pools is the live VM table (grouped by pool) with destroy actions for selected or all VMs, extend for selected VMs, plus a job log while provisioning or destroying runs. A View server logs button at the bottom of the page opens the last 1000 lines of `server.log` in a modal with a search box that filters lines as you type.

## Notes
Prefer a least-privilege Proxmox API token over `root@pam`: a dedicated user with `PVEVMAdmin` on the workshop VMs and template plus `Datastore.AllocateSpace` on the clone storage (and `SDN.Use` on the bridge on PVE 8+) is the usual starting point; confirm with a test deploy.

State lives in `pool.json` (claims + per-VM credentials) and `configs.json` (saved pools, keyed by pool code); both are gitignored. On startup the server re-keys `configs.json` by pool code (regenerating any duplicates) and migrates `pool.json` entries that still reference pools by name. Server, provisioning, and teardown output also goes to `server.log`; set `LOG_FILE` to change the location.
