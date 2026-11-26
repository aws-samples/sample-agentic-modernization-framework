#!/bin/bash
# CodeDeploy ApplicationStop for {{app_name}}: stop the service before the new
# release is copied in. A first deploy (no unit yet) is not an error.
set -euo pipefail
SERVICE="{{service_name}}"
if systemctl list-unit-files "${SERVICE}.service" --no-legend | grep -q "${SERVICE}"; then
  systemctl stop "${SERVICE}"
else
  echo "systemd unit ${SERVICE}.service not found - nothing to stop"
fi
