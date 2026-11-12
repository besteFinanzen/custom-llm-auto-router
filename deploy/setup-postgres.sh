#!/usr/bin/env bash
# Provision the project's local PostgreSQL instance without touching system PostgreSQL.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME=llm-auto-router-postgres
PORT="${ROUTER_POSTGRES_PORT:-55432}"
IMAGE=postgres:18-alpine

if ! docker info >/dev/null 2>&1; then
  echo "Docker is required. Start Docker and ensure your user can access it." >&2
  exit 1
fi

if ! docker container inspect "$NAME" >/dev/null 2>&1; then
  PASSWORD="${ROUTER_DB_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
  docker volume create llm-auto-router-postgres >/dev/null
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "127.0.0.1:${PORT}:5432" \
    -e POSTGRES_DB=router -e POSTGRES_USER=router \
    -e POSTGRES_PASSWORD="$PASSWORD" \
    -v llm-auto-router-postgres:/var/lib/postgresql \
    "$IMAGE" >/dev/null
else
  PASSWORD="${ROUTER_DB_PASSWORD:-}"
  docker start "$NAME" >/dev/null
fi

for _ in $(seq 1 60); do
  if docker exec "$NAME" pg_isready -U router -d router >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! docker exec "$NAME" pg_isready -U router -d router >/dev/null 2>&1; then
  echo "PostgreSQL did not become ready" >&2
  exit 1
fi

# Update only the connection setting; never replace the user's provider keys.
python3 - "$ROOT/.env" "$PORT" "$PASSWORD" <<'PY'
from pathlib import Path
import sys
from urllib.parse import quote

path, port, password = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text().splitlines() if path.exists() else []
prefix = "ROUTER_DATABASE_URL="
if password:
    value = f"postgresql://router:{quote(password, safe='')}@127.0.0.1:{port}/router"
    if any(line.startswith(prefix) for line in lines):
        lines = [prefix + value if line.startswith(prefix) else line for line in lines]
    else:
        lines += ["", "# Durable usage and work-event store (deploy/setup-postgres.sh).", prefix + value]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
elif not any(line.startswith(prefix) for line in lines):
    raise SystemExit("Existing container: set ROUTER_DATABASE_URL in .env, or ROUTER_DB_PASSWORD when rerunning setup.")
PY

echo "PostgreSQL 18 is ready: database router on 127.0.0.1:${PORT}"
echo "Stored ROUTER_DATABASE_URL in ${ROOT}/.env (not committed)."
