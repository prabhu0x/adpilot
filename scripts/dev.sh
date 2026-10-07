#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:${API_PORT}/api/v1}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKEND_VENV="$ROOT/backend/.venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then echo "Python 3 is required." >&2; exit 1; fi
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then echo "Node.js 20+ and npm are required." >&2; exit 1; fi

if [[ ! -x "$BACKEND_VENV/bin/uvicorn" ]]; then
  echo "Preparing backend virtual environment…"
  "$PYTHON_BIN" -m venv "$BACKEND_VENV"
  "$BACKEND_VENV/bin/python" -m pip install --upgrade pip
  "$BACKEND_VENV/bin/pip" install -r "$ROOT/backend/requirements.txt"
fi
if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  echo "Installing frontend dependencies…"
  (cd "$ROOT/frontend" && npm ci)
fi

mkdir -p "$ROOT/backend/var"
export ADPILOT_FORCE_SQLITE="${ADPILOT_FORCE_SQLITE:-1}"
export ADPILOT_SEED_ON_START="${ADPILOT_SEED_ON_START:-1}"
export DATABASE_URL="${DATABASE_URL:-sqlite+pysqlite:///$ROOT/backend/var/adpilot.db}"

cleanup() {
  echo "Stopping AdPilot services…"
  [[ -z "${WEB_PID:-}" ]] || kill "$WEB_PID" 2>/dev/null || true
  [[ -z "${API_PID:-}" ]] || kill "$API_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(
  cd "$ROOT/backend"
  exec "$BACKEND_VENV/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$API_PORT"
) &
API_PID=$!

printf "Waiting for API"
API_STARTUP_TIMEOUT_SECONDS="${API_STARTUP_TIMEOUT_SECONDS:-300}"
for elapsed in $(seq 1 "$API_STARTUP_TIMEOUT_SECONDS"); do
  if curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then break; fi
  if (( elapsed % 15 == 0 )); then
    printf "\nStill preparing seeded analytics (%ss elapsed)…" "$elapsed"
  else
    printf "."
  fi
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then
  printf "\nBackend failed to start within %ss; check the startup output above.\n" "$API_STARTUP_TIMEOUT_SECONDS" >&2
  exit 1
fi
echo " ready"

(
  cd "$ROOT/frontend"
  export NEXT_PUBLIC_API_URL="$API_URL" PORT="$WEB_PORT"
  exec npm run dev
) &
WEB_PID=$!
echo "AdPilot frontend: http://localhost:${WEB_PORT}"
echo "API docs:         http://localhost:${API_PORT}/docs"
echo "Sign in with admin@adpilot.io / AdPilot!2026 (demo only)."
wait "$WEB_PID"
