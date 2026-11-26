#!/usr/bin/env bash
set -euo pipefail
#
# Local dev launcher — runs the FastAPI backend + Vite frontend on your machine,
# talking to the AWS data plane provisioned by scripts/setup.sh (DynamoDB, S3,
# Step Functions, AgentCore). This does NOT deploy anything.
#
# The default GRAPH_BACKEND=dynamodb derives the graph from the table — no containers needed.
#
# Usage: scripts/start.sh [backend|frontend|all]   (default: all)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

cd "$PROJECT_ROOT"

# shellcheck source=/dev/null
if [[ -f .env ]]; then set -a; source .env; set +a; fi

info "── AMF Local Dev ──  (backend + frontend local → AWS data plane)"

# The setup script writes these values to .env. Fail closed rather than silently
# deriving resource names from the generic "amf" prefix and reading the wrong
# table/state machine.
: "${PROJECT_PREFIX:?PROJECT_PREFIX is required in .env (run scripts/setup.sh)}"
export DYNAMODB_TABLE="${DYNAMODB_TABLE:-${PROJECT_PREFIX}_portfolio}"
export SFN_STATE_MACHINE_NAME="${SFN_STATE_MACHINE_NAME:-${PROJECT_PREFIX}-agentcore-workflow}"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export DEV_AUTH="${DEV_AUTH:-true}"
export VITE_DEV_AUTH="${VITE_DEV_AUTH:-true}"
export VITE_API_PROXY_TARGET="${VITE_API_PROXY_TARGET:-http://127.0.0.1:8000}"

if [[ -n "${AMF_PYTHON:-}" && -x "${AMF_PYTHON}" ]]; then
  PYTHON="$AMF_PYTHON"
elif [[ -x backend/.venv/bin/python ]]; then
  PYTHON="backend/.venv/bin/python"
else
  echo "Python environment not found. Run scripts/setup.sh or set AMF_PYTHON to a compatible venv Python."
  exit 1
fi

if ! aws sts get-caller-identity --output text >/dev/null 2>&1; then
  echo "AWS credentials are unavailable or expired. Refresh the configured profile before starting AMF."
  exit 1
fi

info "  Prefix:        $PROJECT_PREFIX"
info "  DynamoDB:      $DYNAMODB_TABLE"
info "  State machine: $SFN_STATE_MACHINE_NAME"
info "  API proxy:     $VITE_API_PROXY_TARGET"

kill_port() {
  local port="$1" pids pid
  pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  [[ -z "$pids" ]] && return 0
  warn "Stopping existing listener(s) on port $port: $pids"
  for pid in $pids; do kill "$pid" 2>/dev/null || true; done
  for _ in {1..20}; do
    lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || return 0
    sleep 0.1
  done
  warn "Listener on port $port did not stop gracefully; forcing shutdown"
  for pid in $pids; do kill -9 "$pid" 2>/dev/null || true; done
}
kill_port 8000
kill_port 3000

MODE="${1:-all}"

start_backend() {
  info "Backend → http://127.0.0.1:8000  (Swagger: /docs, Liveness: /health, Readiness: /health/ready)"
  "$PYTHON" -m uvicorn backend.src.main:app --reload --host 127.0.0.1 --port 8000
}

# Vite 5+/pnpm 9 need Node >= 22. A version manager shim (mise/nvm/asdf) often
# puts an older Node first on PATH, which fails with a bare
# ERR_UNKNOWN_BUILTIN_MODULE that looks like a project bug rather than a Node
# mismatch. Prefer a new-enough Node from a known location when that happens.
ensure_node() {
  local major
  major="$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1)"
  if [ -n "$major" ] && [ "$major" -ge 22 ] 2>/dev/null; then
    return 0
  fi
  local candidate
  for candidate in /opt/homebrew/bin /usr/local/bin; do
    if [ -x "$candidate/node" ]; then
      local cand_major
      cand_major="$("$candidate/node" -v 2>/dev/null | sed 's/^v//' | cut -d. -f1)"
      if [ -n "$cand_major" ] && [ "$cand_major" -ge 22 ] 2>/dev/null; then
        export PATH="$candidate:$PATH"
        info "Using Node $("$candidate/node" -v) from $candidate (default was ${major:-none}, needs >= 22)"
        return 0
      fi
    fi
  done
  info "WARNING: Node ${major:-none} detected and no Node >= 22 found — the frontend may fail to start."
}

# pnpm may be missing or refuse the local Node; vite via npx still works.
run_frontend_dev() {
  ensure_node
  export VITE_DEV_AUTH VITE_API_PROXY_TARGET
  if command -v pnpm >/dev/null 2>&1 && pnpm dev -- --host 127.0.0.1; then
    return 0
  fi
  info "pnpm dev unavailable or failed — falling back to npx vite"
  npx vite --host 127.0.0.1 --port 3000
}

start_frontend() {
  info "Frontend → http://localhost:3000"
  cd frontend && run_frontend_dev
}
start_all() {
  "$PYTHON" -m uvicorn backend.src.main:app --reload --host 127.0.0.1 --port 8000 &
  BACKEND_PID=$!
  local live=false
  for _ in {1..20}; do
    if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
      live=true
      break
    fi
    sleep 1
  done
  if [[ "$live" != true ]]; then
    warn "Backend did not become live"
    kill "$BACKEND_PID" 2>/dev/null || true
    exit 1
  fi
  if ! curl -sf http://127.0.0.1:8000/health/ready >/tmp/amf-readiness.json; then
    warn "Backend is live but AWS dependencies are not ready:"
    cat /tmp/amf-readiness.json 2>/dev/null || true
    kill "$BACKEND_PID" 2>/dev/null || true
    exit 1
  fi
  info "Backend ready (PID: $BACKEND_PID)"

  ( cd frontend && run_frontend_dev ) &
  FRONTEND_PID=$!
  echo ""
  info "=== Running ==="
  info "  Backend:  http://127.0.0.1:8000 (PID: $BACKEND_PID)"
  info "  Frontend: http://127.0.0.1:3000 (PID: $FRONTEND_PID)"
  info "  Ctrl+C to stop both"
  trap 'kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true; exit 0' INT TERM EXIT
  wait
}

case "$MODE" in
  backend)  start_backend ;;
  frontend) start_frontend ;;
  all)      start_all ;;
  *) echo "Usage: scripts/start.sh [backend|frontend|all]"; exit 1 ;;
esac
