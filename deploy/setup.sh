#!/usr/bin/env bash
# Provision guacamole 1.6.0 through Docker, sets up its json extension, then sets up and starts workshop-vm as a systemd service.
# Run with sudo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_DIR="${APP_DIR:-/opt/workshop-vm}"
GUAC_STACK_DIR="${GUAC_STACK_DIR:-${APP_DIR}/guacamole_stack}"
REPO_CLONE_URL="${REPO_CLONE_URL:-https://github.com/hnasheralneam/workshop-vm-distribution.git}"

# Version tag for the Guacamole stack + initdb generation
GUAC_IMAGE_TAG="${GUAC_IMAGE_TAG:-guacamole/guacamole:1.6.0}"
GUAC_GUACD_TAG="${GUAC_GUACD_TAG:-guacamole/guacd:1.6.0}"

# Guacamole Postgres
GUAC_DB_NAME="${GUAC_DB_NAME:-guacamole_db}"
GUAC_DB_USER="${GUAC_DB_USER:-guacamole_user}"
GUAC_DB_PASS="${GUAC_DB_PASS:-WorkshopDBPass123!}"

# random secret key
GUAC_JSON_KEY="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-32)"

# Proxmox + token target
PROXMOX_URL="${PROXMOX_URL:-10.0.0.150}"
PROXMOX_USER="${PROXMOX_USER:-root@pam}"
PROXMOX_SOURCE_NODE="${PROXMOX_SOURCE_NODE:-proxmox}"
PROXMOX_TOKEN_NAME="${PROXMOX_TOKEN_NAME:-}"
PROXMOX_TOKEN_SECRET="${PROXMOX_TOKEN_SECRET:-}"

# Guacamole url is used to mint tokens, the public url is for students
INTERNAL_GUAC_URL="${INTERNAL_GUAC_URL:-http://127.0.0.1:8080/guacamole}"
PUBLIC_GUAC_URL="${PUBLIC_GUAC_URL:-http://127.0.0.1:8080/guacamole}"

# Random admin password
ADMIN_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-20)"
ADMIN_PASSWORD_GENERATED=1
PORT="${PORT:-5000}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
mkdir -p "$APP_DIR" "$GUAC_STACK_DIR"

log() { echo "==> $*"; }

ask_var() {
  local name="$1" label="$2" secret="${3:-}" input="" prompt="$label"
  [[ -t 0 ]] || return 0
  if [[ -n "$secret" ]]; then
    read -rs -p "$prompt: " input; echo
  else
    [[ -n "${!name:-}" ]] && prompt="$label [${!name}]"
    read -r -p "$prompt: " input
  fi
  [[ -n "$input" ]] && printf -v "$name" '%s' "$input"
}

# ---------------------------------------------------------------------------
# 01 · system packages: docker + compose + auth-json zip + python venv
# ---------------------------------------------------------------------------
module_system_pkgs() {
  DEBIAN_FRONTEND=noninteractive apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates python3-venv python3-pip git \
                   apt-transport-https ca-certificates curl gnupg lsb-release
  if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh || {
      DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2; }
  fi
  command -v docker >/dev/null || { echo "docker missing" >&2; return 1; }
}

# ---------------------------------------------------------------------------
# 02 · Guacamole compose stack (guacd + postgres + guacamole) + JSON auth
# ---------------------------------------------------------------------------
module_guac_compose() {
  local initdb_dst="$GUAC_STACK_DIR/initdb.sql"
  # Generate the Postgres schema
  docker run --rm "$GUAC_IMAGE_TAG" /opt/guacamole/bin/initdb.sh --postgresql \
    > "$initdb_dst"
  [[ -s "$initdb_dst" ]] || { echo "initdb.sql generation failed" >&2; return 1; }
  mkdir -p "$GUAC_STACK_DIR/guacamole_home/extensions"

  # The stock guacamole:1.6.0 image auto-links guacamole-auth-json.jar into
  # the runtime home when JSON_SECRET_KEY is set
  sed -e "s|__GUAC_DB_NAME__|${GUAC_DB_NAME}|g" \
      -e "s|__GUAC_DB_USER__|${GUAC_DB_USER}|g" \
      -e "s|__GUAC_DB_PASS__|${GUAC_DB_PASS}|g" \
      -e "s|__GUAC_JSON_KEY__|${GUAC_JSON_KEY}|g" \
      "$SCRIPT_DIR/../guacamole-docker-compose.yaml" > "$GUAC_STACK_DIR/compose.yaml"
}

module_guac_up() {
  cd "$GUAC_STACK_DIR"
  docker compose up -d --remove-orphans
  local i ok=""
  for i in $(seq 1 30); do
    if docker compose ps 2>/dev/null | grep -q guacamole && \
       docker logs guacamole 2>/dev/null | grep -q "Encrypted JSON Authentication"; then
      ok=1; break
    fi
    sleep 3
  done
  [[ -n "$ok" ]] || { echo "guac did not come up / JSON auth not loaded" >&2; return 1; }
  log "Guacamole up on :8080, JSON auth loaded."
}

# ---------------------------------------------------------------------------
# 03 · workshop-vm-distribution app (clone, venv, dependencies)
# ---------------------------------------------------------------------------
module_app_clone() {
  git clone --depth 1 "$REPO_CLONE_URL" "$APP_DIR/workshop-vm-distribution"
}

module_app_venv() {
  cd "$APP_DIR/workshop-vm-distribution"
  if [[ ! -d venv ]]; then python3 -m venv venv; fi
  ./venv/bin/pip install --upgrade pip >/dev/null
  ./venv/bin/pip install -r requirements.txt
}

# ---------------------------------------------------------------------------
# 04 · .env: proxmox + guacamole defaults the code reads via load_dotenv().
# ---------------------------------------------------------------------------
write_env() {
  local path="$1"
  {
    echo "PROXMOX_URL=\"$PROXMOX_URL\""
    echo "PROXMOX_USER=\"$PROXMOX_USER\""
    if [[ -n "${PROXMOX_TOKEN_NAME:-}" ]]; then echo "PROXMOX_TOKEN_NAME=\"$PROXMOX_TOKEN_NAME\""; fi
    if [[ -n "${PROXMOX_TOKEN_SECRET:-}" ]]; then echo "PROXMOX_TOKEN_SECRET=\"$PROXMOX_TOKEN_SECRET\""; fi
    echo "PROXMOX_NODE=\"$PROXMOX_SOURCE_NODE\""
    echo "VERIFY_SSL=false"
    echo
    echo "# guacamole"
    echo "GUACAMOLE_URL=\"$PUBLIC_GUAC_URL\""
    echo "GUACAMOLE_INTERNAL_URL=\"$INTERNAL_GUAC_URL\""
    echo "GUACAMOLE_KEY=\"$GUAC_JSON_KEY\""
    echo
    echo "URL_OUTPUT_FILE=\"pool.json\""
    echo "PORT=$PORT"
    echo "LOG_FILE=\"server.log\""
    if [[ -n "$ADMIN_PASSWORD" ]]; then echo "ADMIN_PASSWORD=\"$ADMIN_PASSWORD\""; fi
  } > "$path"
  chmod 600 "$path"
}

module_env() {
  write_env "$APP_DIR/workshop-vm-distribution/.env"
}

# ---------------------------------------------------------------------------
# 05 · systemd unit: render the checked-in template, enable + start.
# ---------------------------------------------------------------------------
module_systemd() {
  local app_root="$APP_DIR/workshop-vm-distribution"
  sed -e "s|__APP_ROOT__|$app_root|g" -e "s|__PORT__|$PORT|g" \
    "$SCRIPT_DIR/workshop-vm.service" > /etc/systemd/system/workshop-vm.service
  chmod 644 /etc/systemd/system/workshop-vm.service
  systemctl daemon-reload
  systemctl enable workshop-vm
  systemctl restart workshop-vm
  log "portal: http://$HOSTNAME:$PORT  admin: /admin"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
ask_var PROXMOX_URL "Proxmox host"
ask_var PROXMOX_USER "Proxmox user"
ask_var PROXMOX_TOKEN_NAME "Proxmox API token name (part after ! in the token ID)"
ask_var PROXMOX_TOKEN_SECRET "Proxmox API token secret" secret
ask_var PROXMOX_SOURCE_NODE "Proxmox node"
ask_var PUBLIC_GUAC_URL "Public Guacamole base URL for students"

if [[ -z "$PROXMOX_TOKEN_NAME" || -z "$PROXMOX_TOKEN_SECRET" ]]; then
  echo "PROXMOX_TOKEN_NAME and PROXMOX_TOKEN_SECRET are required (export them or run interactively)" >&2
  exit 1
fi

module_system_pkgs
module_guac_compose
module_guac_up
module_app_clone
module_app_venv
module_env
module_systemd

log "Deployment complete."
log "  Guacamole :5000 portal (students) + :8080 guac gui (guacamole/guacamole)"
log "  .env: $APP_DIR/workshop-vm-distribution/.env"
echo "GUAC_JSON_KEY_HINT=$GUAC_JSON_KEY"
if [[ -n "${ADMIN_PASSWORD_GENERATED:-}" ]]; then
  echo "ADMIN_PASSWORD (generated, shown once) = $ADMIN_PASSWORD"
fi
