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
sudo rm -f /etc/systemd/system/cou-flights-fetch.service /etc/systemd/system/cou-flights-fetch.timer
sudo systemctl daemon-reload

echo "== 2/3: HA dashboard + package files =="
rm -f "${HA_CONFIG_DIR}/lovelace/cou_flights.yaml" "${HA_CONFIG_DIR}/packages/cou_flights.yaml"

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
