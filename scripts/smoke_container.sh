#!/usr/bin/env bash
set -euo pipefail

image=${1:-assetboard:ci}
container=assetboard-ci-smoke
volume="assetboard-ci-data-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"

cleanup() {
  docker rm --force "$container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_for_endpoint() {
  local path=$1
  for attempt in $(seq 1 60); do
    if curl --fail --silent "${endpoint_base}${path}" >/dev/null; then
      echo "${path} is ready (attempt ${attempt})."
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${path}." >&2
  docker logs "$container" >&2 || true
  return 1
}

docker volume create "$volume" >/dev/null
docker run --detach \
  --name "$container" \
  --publish 127.0.0.1::8000 \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,source=${volume},target=/data" \
  --env ASSETBOARD_DATA_DIR=/data \
  --env ASSETBOARD_ENV=prod \
  --env ASSETBOARD_AUTO_MIGRATE=true \
  "$image" >/dev/null
host_port=$(docker port "$container" 8000/tcp | sed 's/.*://')
endpoint_base="http://127.0.0.1:${host_port}"

wait_for_endpoint /healthz
wait_for_endpoint /readyz

echo "Checking runtime dependencies and timezone database."
docker exec "$container" python -c \
  'import importlib.util; assert importlib.util.find_spec("pytest") is None, "pytest is installed in runtime image"'
docker exec "$container" python -c \
  'from zoneinfo import ZoneInfo; ZoneInfo("Europe/Warsaw")'

echo "Recording secret and database persistence markers."
secret_before=$(docker exec "$container" sha256sum /data/secret_key | cut -d ' ' -f 1)
docker exec "$container" python -c \
  'import sqlite3; db=sqlite3.connect("/data/assetboard.db"); db.execute("CREATE TABLE IF NOT EXISTS ci_smoke_persistence (marker TEXT PRIMARY KEY)"); db.execute("INSERT OR REPLACE INTO ci_smoke_persistence VALUES (?)", ("survives-restart",)); db.commit(); db.close()'

echo "Restarting the container."
docker restart --time 2 "$container" >/dev/null
host_port=$(docker port "$container" 8000/tcp | sed 's/.*://')
endpoint_base="http://127.0.0.1:${host_port}"
wait_for_endpoint /healthz
wait_for_endpoint /readyz
secret_after=$(docker exec "$container" sha256sum /data/secret_key | cut -d ' ' -f 1)
test "$secret_before" = "$secret_after"
docker exec "$container" python -c \
  'import sqlite3; db=sqlite3.connect("/data/assetboard.db"); assert db.execute("SELECT marker FROM ci_smoke_persistence").fetchone() == ("survives-restart",); db.close()'

echo "secret_key persisted across restart."
echo "Database marker persisted across restart."
echo "Runtime image does not include pytest."
docker logs "$container"
