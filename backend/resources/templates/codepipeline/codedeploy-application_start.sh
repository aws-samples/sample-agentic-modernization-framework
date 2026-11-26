#!/bin/bash
# CodeDeploy ApplicationStart for {{app_name}}: (re)start the systemd unit that
# runs the app from {{install_dir}}. The unit comes with the AMI (AWS MGN /
# golden image); fail the deployment loudly if it is missing.
set -euo pipefail
SERVICE="{{service_name}}"
if ! systemctl list-unit-files "${SERVICE}.service" --no-legend | grep -q "${SERVICE}"; then
  echo "systemd unit ${SERVICE}.service not found on $(hostname): set the app's service_name or bake the unit into the AMI" >&2
  exit 1
fi
systemctl daemon-reload
systemctl restart "${SERVICE}"
