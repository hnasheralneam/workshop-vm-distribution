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
pip install flask proxmoxer requests python-dotenv pycryptodome cryptography
cp .env.example .env
```

Edit `.env` with your Proxmox and Guacamole details:

- `PROXMOX_HOST`, `PROXMOX_USER`, `PROXMOX_TOKEN_NAME`, `PROXMOX_TOKEN_SECRET`, `PROXMOX_NODE`
- `TEMPLATE_VM_ID`, `TEMPLATE_VM_USERNAME`, `TEMPLATE_VM_PASSWORD`
- `GUACAMOLE_URL`, `GUACAMOLE_KEY`
- `URL_OUTPUT_FILE`, `VM_COUNT`

## Usage

1. Provision VMs (writes `pool.json`):
   ```bash
   python workshop-provisioner.py
   ```
2. Start the portal (serves `index.html` on `http://0.0.0.0:5000`, students claim a VM at `/api/claim`):
   ```bash
   python portal_server.py
   ```
3. Tear down all workshop VMs when done (interactive confirmation, only removes `workshop-` prefixed VMs):
   ```bash
   python workshop-destroyer.py
   ```
