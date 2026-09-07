#!/usr/bin/env bash
# Reverse of install.sh. Never touches the database automatically -- dropping
# `flights`/`cou_flights` is a deliberate, manual, data-loss-capable step and
# is only printed as a reminder, not executed.
#
# Usage: ./uninstall.sh <ha-config-dir>
set -euo pipefail

HA_CONFIG_DIR="${1:?Usage: uninstall.sh <ha-config-dir>}"

echo "== 1/3: systemd =="
sudo systemctl disable --now cou-flights-fetch.timer || true
sudo systemctl disable --now n8382a-tracker-fetch.timer || true
sudo rm -f /etc/systemd/system/cou-flights-fetch.service /etc/systemd/system/cou-flights-fetch.timer
sudo rm -f /etc/systemd/system/n8382a-tracker-fetch.service /etc/systemd/system/n8382a-tracker-fetch.timer
sudo systemctl daemon-reload

echo "== 2/3: HA dashboard + package files =="
rm -f "${HA_CONFIG_DIR}/lovelace/cou_flights.yaml" "${HA_CONFIG_DIR}/packages/cou_flights.yaml"

echo "== N8382A tracker map card =="
rm -rf "${HA_CONFIG_DIR}/www/community/mandi-aircraft-tracker"
python3 - "${HA_CONFIG_DIR}/.storage/lovelace_resources" <<'PYEOF'
import json, os, sys

path = sys.argv[1]
BASE_URL = "/local/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js"
if not os.path.exists(path):
    # Nothing was ever registered (fresh host, never had a UI-managed
    # Lovelace resource) -- nothing to remove, skip cleanly.
    sys.exit(0)
with open(path) as f:
    data = json.load(f)
before = len(data["data"]["items"])
data["data"]["items"] = [
    i for i in data["data"]["items"]
    if i["url"].split("?")[0] != BASE_URL
]
if len(data["data"]["items"]) != before:
    with open(path, "w") as f:
        json.dump(data, f)
    print("removed N8382A tracker card lovelace resource")
PYEOF

echo "== 3/3: manual step reminders =="
cat <<'EOF'
Remove the `cou-flights-dashboard` block from homeassistant/config/configuration.yaml,
then:
    docker exec homeassistant python3 -m homeassistant --script check_config --config /config
    docker compose -f ~/homeassistant/docker-compose.yaml restart homeassistant

The `flights` database and `cou_flights` role were left in place -- this
script never drops data automatically. To remove them yourself:
    sudo -u postgres psql -c "DROP DATABASE flights;"
    sudo -u postgres psql -c "DROP ROLE cou_flights;"
    sudo rm -f /etc/mandi/cou-flights.env
EOF
