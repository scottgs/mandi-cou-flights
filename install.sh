#!/usr/bin/env bash
# Install the COU Flights panel: provisions its PostgreSQL role/database/schema
# (only if they don't already exist -- never destructive), installs its systemd
# fetch timer, and copies its Lovelace dashboard + HA package YAML into place.
#
# Usage: ./install.sh <install-user> <repo-dir> <ha-config-dir>
# Example: ./install.sh scottgs /home/scottgs/repos/mandi-cou-flights /home/scottgs/homeassistant/config
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_USER="${1:?Usage: install.sh <install-user> <repo-dir> <ha-config-dir>}"
REPO_DIR="${2:?Usage: install.sh <install-user> <repo-dir> <ha-config-dir>}"
HA_CONFIG_DIR="${3:?Usage: install.sh <install-user> <repo-dir> <ha-config-dir>}"

provision_db() {
  local db_password="$1"
  # Role/database existence are checked in bash, not SQL -- Postgres has no
  # CREATE ROLE/DATABASE IF NOT EXISTS, and psql's -v substitution can't
  # reach inside a DO $$ ... $$ block (the whole block lexes as one opaque
  # string), so a DO-block existence check silently can't see :variables.
  # Note: psql only interpolates -v variables when SQL arrives over stdin,
  # not via -c "...", hence the heredocs below instead of -c.
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='cou_flights'" | grep -q 1; then
    sudo -u postgres psql -v cou_flights_password="'${db_password}'" <<'SQL'
CREATE ROLE cou_flights WITH LOGIN PASSWORD :cou_flights_password;
SQL
  fi
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='flights'" | grep -q 1; then
    sudo -u postgres psql -c "CREATE DATABASE flights OWNER cou_flights;"
  fi
  cat "$SCRIPT_DIR/db/schema.sql" | sudo -u postgres psql -d flights
  echo "flights database provisioned (or already present -- schema.sql is idempotent)"
}

register_lovelace_resource() {
  local ha_config_dir="$1"
  local base_url="$2"
  local resource_url="$3"
  python3 - "${ha_config_dir}/.storage/lovelace_resources" "$base_url" "$resource_url" <<'PYEOF'
import json, os, sys, uuid

path, base_url, url = sys.argv[1], sys.argv[2], sys.argv[3]
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
else:
    # Fresh HA host in YAML-dashboard mode that has never had any
    # UI-managed Lovelace resource -- this file doesn't exist yet.
    # Start with the minimal valid lovelace_resources storage structure.
    data = {"version": 1, "minor_version": 1, "key": "lovelace_resources", "data": {"items": []}}
items = data["data"]["items"]
if any(i["url"] == url for i in items):
    print(f"lovelace resource already registered: {url}")
else:
    before = len(items)
    items[:] = [i for i in items if i["url"].split("?")[0] != base_url]
    removed = before - len(items)
    items.append({"id": uuid.uuid4().hex, "url": url, "type": "module"})
    suffix = f" (replaced {removed} stale version)" if removed else ""
    print(f"registered lovelace resource: {url}{suffix}")
    with open(path, "w") as f:
        json.dump(data, f)
PYEOF
}

ensure_env_var() {
  local key="$1" prompt_text="$2"
  if ! sudo grep -q "^${key}=" /etc/mandi/cou-flights.env 2>/dev/null; then
    read -rsp "${prompt_text}: " value; echo
    echo "${key}=${value}" | sudo tee -a /etc/mandi/cou-flights.env >/dev/null
  fi
}

echo "== 1/5: env file =="
sudo install -d -m 0755 /etc/mandi
sudo touch /etc/mandi/cou-flights.env
sudo chmod 0600 /etc/mandi/cou-flights.env
ensure_env_var COU_FLIGHTS_DB_PASSWORD "COU_FLIGHTS_DB_PASSWORD for new role 'cou_flights'"
ensure_env_var OPENSKY_CLIENT_ID "OPENSKY_CLIENT_ID (from https://opensky-network.org/my-opensky/account)"
ensure_env_var OPENSKY_CLIENT_SECRET "OPENSKY_CLIENT_SECRET (from https://opensky-network.org/my-opensky/account)"
DB_PASSWORD="$(sudo grep -oP '(?<=COU_FLIGHTS_DB_PASSWORD=).*' /etc/mandi/cou-flights.env)"

echo "== 2/5: database =="
provision_db "$DB_PASSWORD"

echo "== 3/5: systemd units =="
sed -e "s|__INSTALL_USER__|${INSTALL_USER}|g" \
    -e "s|__HA_WWW_DIR__|${HA_CONFIG_DIR}/www|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    "$SCRIPT_DIR/systemd/cou-flights-fetch.service" | sudo tee /etc/systemd/system/cou-flights-fetch.service >/dev/null
sudo cp "$SCRIPT_DIR/systemd/cou-flights-fetch.timer" /etc/systemd/system/cou-flights-fetch.timer
sed -e "s|__INSTALL_USER__|${INSTALL_USER}|g" \
    -e "s|__HA_WWW_DIR__|${HA_CONFIG_DIR}/www|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    "$SCRIPT_DIR/systemd/n8382a-tracker-fetch.service" | sudo tee /etc/systemd/system/n8382a-tracker-fetch.service >/dev/null
sudo cp "$SCRIPT_DIR/systemd/n8382a-tracker-fetch.timer" /etc/systemd/system/n8382a-tracker-fetch.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cou-flights-fetch.timer
sudo systemctl enable --now n8382a-tracker-fetch.timer

echo "== 4/5: HA dashboard + package files =="
mkdir -p "${HA_CONFIG_DIR}/www/cou_flights"
mkdir -p "${HA_CONFIG_DIR}/www/community/mandi-aircraft-tracker"
cp "$SCRIPT_DIR/ha/www/community/mandi-aircraft-tracker/"*.js "$SCRIPT_DIR/ha/www/community/mandi-aircraft-tracker/"*.css \
  "${HA_CONFIG_DIR}/www/community/mandi-aircraft-tracker/"
CARD_JS_BASE_URL="/local/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js"
CARD_JS_HASH="$(sha256sum "$SCRIPT_DIR/ha/www/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js" | cut -c1-8)"
register_lovelace_resource "${HA_CONFIG_DIR}" "${CARD_JS_BASE_URL}" "${CARD_JS_BASE_URL}?v=${CARD_JS_HASH}"
cp "$SCRIPT_DIR/ha/lovelace/cou_flights.yaml" "${HA_CONFIG_DIR}/lovelace/cou_flights.yaml"
cp "$SCRIPT_DIR/ha/packages/cou_flights.yaml" "${HA_CONFIG_DIR}/packages/cou_flights.yaml"

echo "== 5/5: manual step reminder =="
cat <<'EOF'
Add this dashboard entry to homeassistant/config/configuration.yaml under
`lovelace: dashboards:` if not already present:

    cou-flights-dashboard:
      mode: yaml
      title: COU Flights
      icon: mdi:airplane
      show_in_sidebar: true
      filename: lovelace/cou_flights.yaml

Then:
    docker exec homeassistant python3 -m homeassistant --script check_config --config /config
    docker compose -f ~/homeassistant/docker-compose.yaml restart homeassistant
EOF
