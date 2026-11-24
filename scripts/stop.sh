#!/usr/bin/env bash
set -euo pipefail
#
# Stop the local dev services started by scripts/start.sh (backend :8000,
# frontend :3000).

GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }

info "Stopping local services..."

for port in 8000 3000; do
  pid=$(lsof -ti :"$port" 2>/dev/null || true)
  if [[ -n "$pid" ]]; then
    echo "$pid" | xargs kill -9 2>/dev/null || true
    info "  Stopped process on port $port (PID: $pid)"
  else
    info "  Nothing on port $port"
  fi
done


echo ""
info "All local services stopped."
