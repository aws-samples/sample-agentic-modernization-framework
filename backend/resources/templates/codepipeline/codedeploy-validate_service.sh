#!/bin/bash
# CodeDeploy ValidateService for {{app_name}}: the instance only rejoins the ALB
# target group once the health path answers 200 (CodeDeploy rolls back otherwise).
set -uo pipefail
URL="http://127.0.0.1:{{container_port}}{{health_check_path}}"
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL" || true)
  if [ "$CODE" = "200" ]; then echo "Health check passed ($URL)"; exit 0; fi
  echo "Attempt $i: $URL -> ${CODE:-no response}"; sleep 10
done
echo "Health check failed: $URL" >&2
exit 1
