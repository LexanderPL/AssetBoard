#!/usr/bin/env bash
# Repeatable release checks for a published image (F9.8).
#
# The checks use disposable Docker volumes and never touch a real data directory. They need
# Docker, curl and Python 3 on the host; the image supplies them for the work inside containers.
#
#   scripts/release_checks.sh fresh-install IMAGE [--keep]
#   scripts/release_checks.sh upgrade PREVIOUS_IMAGE IMAGE [--require-migration] [--keep]
#   scripts/release_checks.sh backup-restore IMAGE [--keep]
#   scripts/release_checks.sh all PREVIOUS_IMAGE IMAGE [--require-migration] [--keep]
#   scripts/release_checks.sh verify-tags IMAGE_REPOSITORY TAG
#
# IMAGE may be a local tag (assetboard:local) or a registry image
# (ghcr.io/lexanderpl/assetboard:1.0.0). `verify-tags` inspects an already-published
# repository and needs a registry login for a private package.
#
# `upgrade --require-migration` additionally fails when the two images already share a schema
# revision. Use it for the forward-only migration check with an older tag that predates the
# newest migration; the plain `upgrade` checks the immediately previous release.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
compose_file=$repo_root/compose.example.yaml

suffix=${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$
keep=false
work_dir=
temp_dirs=()
container=
endpoint_base=
cookie_jar=
response_file=
containers=()
volumes=()
projects=()

usage() {
  cat <<'EOF'
Usage:
  scripts/release_checks.sh fresh-install IMAGE [--keep]
  scripts/release_checks.sh upgrade PREVIOUS_IMAGE IMAGE [--require-migration] [--keep]
  scripts/release_checks.sh backup-restore IMAGE [--keep]
  scripts/release_checks.sh all PREVIOUS_IMAGE IMAGE [--require-migration] [--keep]
  scripts/release_checks.sh verify-tags IMAGE_REPOSITORY TAG

IMAGE may be a local tag (assetboard:local) or a registry image. The fresh-install check uses
compose.example.yaml and a disposable Compose project; the other checks use disposable Docker
volumes. Nothing touches a real data directory. verify-tags inspects an already-published
repository, requires both linux/amd64 and linux/arm64, and needs a registry login for a private
package. --require-migration fails when the two images already share a schema revision; use an
older tag that predates the newest migration for the forward-only check. --keep leaves the
container, volume and Compose project running for a manual page/sign-in check and prints how to
remove them.
EOF
}

log() {
  printf '==> %s\n' "$*"
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

ensure_image() {
  local image=$1
  if [[ $image == */* ]]; then
    log "Pulling ${image}."
    docker pull "$image" >/dev/null || fail "cannot pull ${image}"
  fi
}

require_commands() {
  local command
  for command in docker curl python3; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required"
  done
  docker info >/dev/null 2>&1 || fail "the Docker daemon is not reachable"
}

register_container() {
  containers+=("$1")
}

register_volume() {
  volumes+=("$1")
}

register_project() {
  projects+=("$1")
}

cleanup() {
  local status=$?
  trap - EXIT
  if (( status != 0 )); then
    if [[ -n ${container:-} ]] && docker inspect "$container" >/dev/null 2>&1; then
      printf '%s\n' "--- logs: $container" >&2
      docker logs "$container" >&2 2>&1 || true
    fi
    local name
    for name in "${containers[@]}"; do
      if docker inspect "$name" >/dev/null 2>&1; then
        printf '%s\n' "--- logs: $name" >&2
        docker logs "$name" >&2 2>&1 || true
      fi
    done
  fi
  if [[ $keep == true ]]; then
    printf '%s\n' "==> Keeping the release containers/volumes for inspection." >&2
    [[ -n ${container:-} ]] && printf 'container: %s\n' "$container" >&2
    [[ -n ${endpoint_base:-} ]] && printf 'URL: %s\n' "$endpoint_base" >&2
    for project in "${projects[@]}"; do
      printf 'compose project: %s\n' "$project" >&2
      printf '  remove: ASSETBOARD_IMAGE=assetboard:cleanup ASSETBOARD_HOST_PORT=18000 docker compose -p %s -f %s down --volumes --remove-orphans\n' \
        "$project" "$compose_file" >&2
    done
    for volume in "${volumes[@]}"; do printf 'volume: %s\n' "$volume" >&2; done
    for dir in "${temp_dirs[@]}"; do printf 'work dir: %s\n' "$dir" >&2; done
    printf 'Remove with: docker rm -f %s; docker volume rm %s; %s\n' \
      "${containers[*]:-(none)}" "${volumes[*]:-(none)}" \
      "$(printf 'rm -rf %q; ' "${temp_dirs[@]}" 2>/dev/null || printf 'true')" >&2
    exit "$status"
  fi
  local project name volume
  for project in "${projects[@]}"; do
    ASSETBOARD_IMAGE=assetboard:cleanup ASSETBOARD_HOST_PORT=18000 \
      docker compose -p "$project" -f "$compose_file" down --volumes --remove-orphans \
      >/dev/null 2>&1 || true
  done
  for name in "${containers[@]}"; do
    docker rm --force "$name" >/dev/null 2>&1 || true
  done
  for volume in "${volumes[@]}"; do
    docker volume rm "$volume" >/dev/null 2>&1 || true
  done
  for dir in "${temp_dirs[@]}"; do
    rm -rf "$dir"
  done
  exit "$status"
}
trap cleanup EXIT

new_work_dir() {
  work_dir=$(mktemp -d "${TMPDIR:-/tmp}/assetboard-release-$1.XXXXXX")
  temp_dirs+=("$work_dir")
}

free_port() {
  python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

wait_for_endpoint() {
  local path=$1 attempts=${2:-60} attempt state
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    state=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)
    if [[ $state == exited || $state == dead ]]; then
      fail "container $container stopped while waiting for ${path}"
    fi
    if curl --fail --silent --connect-timeout 5 --max-time 15 "${endpoint_base}${path}" >/dev/null 2>&1; then
      log "${path} is ready (attempt ${attempt})."
      return 0
    fi
    sleep 2
  done
  fail "timed out waiting for ${path}"
}

start_container() {
  local name=$1 image=$2 volume=$3 auto_migrate=$4
  docker run --detach \
    --name "$name" \
    --publish 127.0.0.1::8000 \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=volume,source=${volume},target=/data" \
    --env ASSETBOARD_DATA_DIR=/data \
    --env ASSETBOARD_ENV=prod \
    --env ASSETBOARD_AUTO_MIGRATE="$auto_migrate" \
    --env ASSETBOARD_SCHEDULER_ENABLED=false \
    --env ASSETBOARD_TRUSTED_PROXIES= \
    "$image" >/dev/null
  register_container "$name"
  container=$name
  local host_port
  host_port=$(docker port "$container" 8000/tcp | sed 's/.*://')
  [[ -n $host_port ]] || fail "could not read the published port for $container"
  endpoint_base="http://127.0.0.1:${host_port}"
}

wait_for_app() {
  wait_for_endpoint /healthz
  wait_for_endpoint /readyz
}

http_status() {
  curl --silent --show-error --connect-timeout 5 --max-time 15 --output "$response_file" --write-out '%{http_code} %{redirect_url}' \
    --cookie "$cookie_jar" --cookie-jar "$cookie_jar" "$@"
}

assert_redirect() {
  local path=$1 target=$2 result
  result=$(http_status "${endpoint_base}${path}")
  if [[ $result != "303 ${endpoint_base}${target}" ]]; then
    fail "${path} returned '${result}', expected a 303 to ${target}"
  fi
  log "${path} redirects to ${target}."
}

assert_status() {
  local path=$1 expected=$2 result
  result=$(http_status "${endpoint_base}${path}")
  if [[ ${result%% *} != "$expected" ]]; then
    fail "${path} returned HTTP ${result%% *}, expected ${expected}"
  fi
  log "${path} returned HTTP ${expected}."
}

page_csrf_token() {
  sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$response_file" | head -n 1
}

create_admin() {
  local password=$1
  printf '%s' "$password" | docker exec --interactive "$container" \
    assetboard create-admin --email release-check@example.com --display-name "Release Check" \
    --password-stdin >/dev/null
  log "Created the administrator through the CLI."
}

workspace_id() {
  docker exec "$container" python -c '
import sqlite3

db = sqlite3.connect("/data/assetboard.db")
row = db.execute("SELECT id FROM workspaces ORDER BY created_at LIMIT 1").fetchone()
db.close()
print(row[0] if row else "")
'
}

seed_demo_data() {
  local id
  id=$(workspace_id)
  [[ -n $id ]] || fail "the workspace created by create-admin was not found"
  docker exec "$container" assetboard demo-seed --workspace "$id" --date 2026-01-15
  log "Seeded the deterministic demo workspace."
}

snapshot_script=$(cat <<'PY'
import hashlib
import json
import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/data/assetboard.db"
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    revision = db.execute("SELECT version_num FROM alembic_version").fetchone()
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"
        )
    ]
    counts = {}
    digests = {}
    for table in tables:
        cursor = db.execute(f'SELECT * FROM "{table}"')
        columns = [column[0] for column in cursor.description]
        rows = sorted(
            json.dumps(dict(zip(columns, map(str, row))), sort_keys=True)
            for row in cursor
        )
        counts[table] = len(rows)
        digests[table] = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
finally:
    db.close()
print(
    json.dumps(
        {"revision": revision[0] if revision else None, "tables": counts, "digests": digests},
        sort_keys=True,
    )
)
PY
)

snapshot_running() {
  docker exec "$container" python -c "$snapshot_script" /data/assetboard.db
}

snapshot_volume() {
  local volume=$1 image=$2
  docker run --rm --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
    --mount "type=volume,source=${volume},target=/data" \
    "$image" python -c "$snapshot_script" /data/assetboard.db
}

compare_snapshots() {
  local before=$1 after=$2 label=$3
  python3 - "$before" "$after" "$label" <<'PY'
import json
import sys

before = json.load(open(sys.argv[1], encoding="utf-8"))
after = json.load(open(sys.argv[2], encoding="utf-8"))
label = sys.argv[3]
KEY_TABLES = (
    "users",
    "workspaces",
    "memberships",
    "categories",
    "transactions",
    "budget_lines",
    "month_states",
    "scheduled_items",
    "scheduled_occurrences",
    "income_sources",
    "income_source_tax_rules",
    "work_logs",
    "work_days",
    "tax_rules",
    "tax_rule_versions",
    "networth_categories",
    "networth_items",
    "item_valuations",
    "networth_snapshots",
    "reference_rates",
    "reference_rate_values",
    "asset_classes",
    "instruments",
    "instrument_allocations",
    "investment_transactions",
    "bond_terms",
    "price_points",
    "fx_rates",
    "risk_target_policies",
    "loans",
    "loan_rate_changes",
    "loan_transfers",
    "loan_cost_components",
    "loan_overpayments",
    "loan_balance_checkpoints",
    "loan_payments",
    "fire_scenarios",
    "fire_events",
    "import_batches",
    "import_category_mappings",
)
# Read-only page views may touch login bookkeeping, but never these financial rows.
CONTENT_TABLES = (
    "categories",
    "transactions",
    "budget_lines",
    "scheduled_items",
    "scheduled_occurrences",
    "income_sources",
    "income_source_tax_rules",
    "work_logs",
    "work_days",
    "tax_rules",
    "tax_rule_versions",
    "networth_categories",
    "networth_items",
    "item_valuations",
    "networth_snapshots",
    "reference_rates",
    "reference_rate_values",
    "asset_classes",
    "instruments",
    "instrument_allocations",
    "investment_transactions",
    "bond_terms",
    "price_points",
    "fx_rates",
    "risk_target_policies",
    "loans",
    "loan_rate_changes",
    "loan_transfers",
    "loan_cost_components",
    "loan_overpayments",
    "loan_balance_checkpoints",
    "loan_payments",
    "fire_scenarios",
    "fire_events",
)
for table in KEY_TABLES:
    if table not in before["tables"]:
        continue
    expected = before["tables"][table]
    actual = after["tables"].get(table)
    if actual != expected:
        raise SystemExit(f"{label}: {table} rows changed: {expected} -> {actual}")
for table in CONTENT_TABLES:
    if table not in before["digests"]:
        continue
    if after["digests"].get(table) != before["digests"][table]:
        raise SystemExit(f"{label}: {table} contents changed")
if not after.get("revision"):
    raise SystemExit(f"{label}: no Alembic revision in the database")
print(f"{label}: business rows, contents and schema revision are intact.")
PY
}

assert_login() {
  local email=$1 password=$2 csrf result
  : >"$cookie_jar"
  assert_status /login 200
  csrf=$(page_csrf_token)
  [[ -n $csrf ]] || fail "the login page carried no CSRF token"
  result=$(http_status "${endpoint_base}/login" \
    --data-urlencode "csrf_token=${csrf}" \
    --data-urlencode "email=${email}" \
    --data-urlencode "password=${password}")
  if [[ $result != "303 ${endpoint_base}/" ]]; then
    fail "POST /login returned '${result}', expected a 303 to /"
  fi
  assert_status / 200
  grep -Fq 'Release Check' "$response_file" || fail "the signed-in home page lost the account name"
  log "Signed in with the restored account."
}

secret_hash() {
  docker exec "$container" python -c '
import hashlib
from pathlib import Path

print(hashlib.sha256(Path("/data/secret_key").read_bytes()).hexdigest())
'
}

fresh_install() {
  local image=$1 project port setup_token csrf result icon
  require_commands
  ensure_image "$image"
  new_work_dir fresh
  cookie_jar=$work_dir/cookies.txt
  response_file=$work_dir/response.html
  project="assetboard-release-fresh-${suffix}"
  port=$(free_port)
  register_project "$project"

  log "Fresh install from ${image} through Compose on port ${port}."
  ASSETBOARD_IMAGE=$image \
    ASSETBOARD_HOST_PORT=$port \
    ASSETBOARD_BIND_ADDRESS=127.0.0.1 \
    ASSETBOARD_BASE_URL="http://127.0.0.1:${port}" \
    docker compose -p "$project" -f "$compose_file" up -d --no-build --wait --wait-timeout 120
  container=$(ASSETBOARD_IMAGE=$image docker compose -p "$project" -f "$compose_file" \
    ps -q assetboard)
  [[ -n $container ]] || fail "Compose did not start the assetboard service"
  register_container "$container"
  endpoint_base="http://127.0.0.1:${port}"

  wait_for_endpoint /healthz
  wait_for_endpoint /readyz
  assert_redirect / /setup
  assert_status /setup 200
  grep -Fq 'name="setup_token"' "$response_file" || fail "/setup has no setup token field"

  setup_token=$(docker logs "$container" 2>&1 \
    | grep -o 'Generated first-run setup token: [A-Za-z0-9_-]*' \
    | head -n 1 | awk '{print $NF}')
  [[ -n $setup_token ]] || fail "the container did not log a first-run setup token"
  csrf=$(page_csrf_token)
  [[ -n $csrf ]] || fail "/setup carried no CSRF token"
  result=$(http_status "${endpoint_base}/setup" \
    --data-urlencode "csrf_token=${csrf}" \
    --data-urlencode "setup_token=${setup_token}" \
    --data-urlencode "email=release-check@example.com" \
    --data-urlencode "display_name=Release Check" \
    --data-urlencode "password=Release Check Strong Password")
  if [[ $result != "303 ${endpoint_base}/" ]]; then
    fail "POST /setup returned '${result}', expected a 303 to /"
  fi
  assert_status / 200
  grep -Fq 'Release Check' "$response_file" || fail "the fresh install lost its administrator"
  assert_status /setup 404
  assert_status /readyz 200
  log "First-run setup completed."

  assert_status /offline 200
  curl --fail --silent --show-error "${endpoint_base}/manifest.webmanifest" \
    -o "$work_dir/manifest.json"
  python3 - "$work_dir/manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
assert manifest.get("display") == "standalone", manifest
assert manifest.get("name"), manifest
assert manifest.get("start_url"), manifest
assert len(manifest.get("icons", [])) == 4, manifest
assert {icon.get("purpose") for icon in manifest["icons"]} == {"any", "maskable"}, manifest
print("Web app manifest is installable.")
PY
  curl --fail --silent --show-error "${endpoint_base}/service-worker.js" \
    -o "$work_dir/service-worker.js"
  test -s "$work_dir/service-worker.js" || fail "/service-worker.js is empty"
  for icon in icon-192.png icon-512.png icon-192-maskable.png icon-512-maskable.png \
    apple-touch-icon.png; do
    curl --fail --silent --show-error --output /dev/null \
      "${endpoint_base}/static/img/${icon}"
  done
  log "PWA service worker and icons are present."

  : >"$cookie_jar"
  assert_redirect / /login?next=%2F
  log "Fresh install from ${image} passed."
}

upgrade() {
  local previous=$1 image=$2 require_migration=$3
  local volume old_name new_name before_file after_file upgraded_file
  local before_revision after_revision upgraded_revision
  require_commands
  ensure_image "$previous"
  ensure_image "$image"
  new_work_dir upgrade
  cookie_jar=$work_dir/cookies.txt
  response_file=$work_dir/response.html
  before_file=$work_dir/before.json
  after_file=$work_dir/after.json
  upgraded_file=$work_dir/upgraded.json
  volume="assetboard-release-upgrade-${suffix}"
  old_name="assetboard-release-old-${suffix}"
  new_name="assetboard-release-new-${suffix}"
  docker volume create "$volume" >/dev/null
  register_volume "$volume"

  log "Starting ${previous} with a populated database."
  start_container "$old_name" "$previous" "$volume" true
  wait_for_app
  create_admin "Release Check Strong Password"
  seed_demo_data
  snapshot_running >"$before_file"
  before_revision=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' \
    "$before_file")
  [[ -n $before_revision ]] || fail "the previous release has no Alembic revision"
  log "Previous release schema: ${before_revision}."

  log "Stopping ${previous} and starting ${image} on the same volume."
  docker stop "$container" >/dev/null
  docker rm "$container" >/dev/null
  start_container "$new_name" "$image" "$volume" true
  wait_for_app
  assert_redirect / /login?next=%2F
  assert_login release-check@example.com "Release Check Strong Password"
  snapshot_running >"$after_file"
  after_revision=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' \
    "$after_file")
  log "New release schema: ${after_revision}."
  compare_snapshots "$before_file" "$after_file" "upgrade from ${previous}"
  cp "$after_file" "$upgraded_file"
  upgraded_revision=$after_revision

  if [[ $before_revision == "$after_revision" ]]; then
    log "The two releases already share a schema revision; no migration was applied."
    if [[ $require_migration == true ]]; then
      fail "${previous} is already at ${after_revision}; choose an older tag for --require-migration"
    fi
  else
    docker logs "$container" 2>&1 | grep -Fq 'database migrations applied at startup:' \
      || fail "startup did not log the applied migrations"
    log "Migrations applied: ${before_revision} -> ${after_revision}."

    log "Starting ${previous} against the newer schema (forward-only check)."
    docker stop "$container" >/dev/null
    docker rm "$container" >/dev/null
    start_container "$new_name" "$previous" "$volume" false
    local attempt status=000
    for ((attempt = 1; attempt <= 30; attempt++)); do
      status=$(curl --silent --output "$response_file" --write-out '%{http_code}' \
        "${endpoint_base}/readyz" 2>/dev/null || true)
      [[ $status == 503 ]] && break
      if docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null \
        | grep -Eq 'exited|dead'; then
        fail "the previous release exited instead of reporting a newer schema"
      fi
      sleep 1
    done
    [[ $status == 503 ]] || fail "the previous release answered /readyz with HTTP ${status}"
    grep -Fq 'schema_newer' "$response_file" || fail "/readyz no longer reports schema_newer"
    docker stop "$container" >/dev/null
    docker rm "$container" >/dev/null
    snapshot_volume "$volume" "$image" >"$after_file"
    after_revision=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' \
      "$after_file")
    [[ $after_revision == "$upgraded_revision" ]] \
      || fail "forward-only check changed the revision: ${upgraded_revision} -> ${after_revision}"
    compare_snapshots "$upgraded_file" "$after_file" "forward-only check"
    log "The previous release refused the newer schema without changing the revision."
  fi
  log "Upgrade check from ${previous} to ${image} passed."
}

backup_restore() {
  local image=$1 source_volume restored_volume source_name restored_name
  local before_file after_file before_secret after_secret
  require_commands
  ensure_image "$image"
  new_work_dir restore
  cookie_jar=$work_dir/cookies.txt
  response_file=$work_dir/response.html
  before_file=$work_dir/before.json
  after_file=$work_dir/after.json
  source_volume="assetboard-release-source-${suffix}"
  restored_volume="assetboard-release-restored-${suffix}"
  source_name="assetboard-release-source-${suffix}"
  restored_name="assetboard-release-restored-${suffix}"
  docker volume create "$source_volume" >/dev/null
  register_volume "$source_volume"
  docker volume create "$restored_volume" >/dev/null
  register_volume "$restored_volume"

  log "Creating a populated database with ${image}."
  start_container "$source_name" "$image" "$source_volume" true
  wait_for_app
  create_admin "Release Check Strong Password"
  seed_demo_data
  snapshot_running >"$before_file"
  before_secret=$(secret_hash)

  log "Taking an online SQLite backup and its secret key."
  docker exec "$container" python -c '
import shutil
import sqlite3

source = sqlite3.connect("/data/assetboard.db")
target = sqlite3.connect("/data/roundtrip.db")
source.backup(target)
target.close()
source.close()
shutil.copy2("/data/secret_key", "/data/roundtrip.db.secret_key")
'
  docker stop "$container" >/dev/null
  docker rm "$container" >/dev/null

  # Move the backup and its key sidecar into the clean volume with a read-only mount of the
  # source volume. Both containers run as the image's UID 10001, so no host chmod or data copy
  # is needed.
  docker run --rm --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=volume,source=${source_volume},target=/source,readonly" \
    --mount "type=volume,source=${restored_volume},target=/data" \
    "$image" python -c '
import shutil

for name in ("roundtrip.db", "roundtrip.db.secret_key"):
    shutil.copy2(f"/source/{name}", f"/data/{name}")
'

  log "Restoring the backup into a clean volume with assetboard restore."
  docker run --rm --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=volume,source=${restored_volume},target=/data" \
    "$image" assetboard restore /data/roundtrip.db --data-dir /data

  start_container "$restored_name" "$image" "$restored_volume" true
  wait_for_app
  assert_redirect / /login?next=%2F
  assert_login release-check@example.com "Release Check Strong Password"
  after_secret=$(secret_hash)
  [[ $before_secret == "$after_secret" ]] || fail "the restored secret key differs"
  log "The restored secret key matches the backup sidecar."
  snapshot_running >"$after_file"
  compare_snapshots "$before_file" "$after_file" "backup and restore"
  log "Backup and restore check passed."
}

verify_tags() {
  local repository=$1 tag=$2
  require_commands
  repository=${repository%/}
  [[ ${repository##*/} != *:* ]] \
    || fail "pass the image repository without a tag to verify-tags"
  python3 "$script_dir/verify_release_tags.py" --tag "$tag" --check-remote "$repository" \
    || fail "release tag verification failed"
  log "All expected tags for ${tag} exist with linux/amd64 and linux/arm64 manifests."
}

mode=${1:-}
if [[ -z $mode ]]; then
  usage
  exit 2
fi
shift

case "$mode" in
  fresh-install)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    image=$1
    shift
    for argument in "$@"; do
      case "$argument" in
        --keep) keep=true ;;
        *) fail "unknown option: $argument" ;;
      esac
    done
    fresh_install "$image"
    ;;
  upgrade)
    [[ $# -ge 2 ]] || { usage; exit 2; }
    previous=$1
    image=$2
    shift 2
    require_migration=false
    for argument in "$@"; do
      case "$argument" in
        --require-migration) require_migration=true ;;
        --keep) keep=true ;;
        *) fail "unknown option: $argument" ;;
      esac
    done
    upgrade "$previous" "$image" "$require_migration"
    ;;
  backup-restore)
    [[ $# -ge 1 ]] || { usage; exit 2; }
    image=$1
    shift
    for argument in "$@"; do
      case "$argument" in
        --keep) keep=true ;;
        *) fail "unknown option: $argument" ;;
      esac
    done
    backup_restore "$image"
    ;;
  all)
    [[ $# -ge 2 ]] || { usage; exit 2; }
    previous=$1
    image=$2
    shift 2
    require_migration=false
    for argument in "$@"; do
      case "$argument" in
        --require-migration) require_migration=true ;;
        --keep) keep=true ;;
        *) fail "unknown option: $argument" ;;
      esac
    done
    fresh_install "$image"
    upgrade "$previous" "$image" "$require_migration"
    backup_restore "$image"
    ;;
  verify-tags)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    verify_tags "$1" "$2"
    ;;
  *)
    usage
    exit 2
    ;;
esac
