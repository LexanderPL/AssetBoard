#!/usr/bin/env bash
# Install or upgrade AssetBoard natively on Debian/Ubuntu with systemd.
# Docs: docs/install/native.md. Run as root; safe to run again (it is idempotent).
#
#   install-debian.sh                    install or upgrade to the latest release
#   install-debian.sh --version 0.3.0    install or upgrade to a given release
#
# Each release lives in its own virtual environment under $prefix/releases/<version>;
# $prefix/current points at the active one. An upgrade installs the new release next to the old
# one, stops the service, backs up the database and the secret key, switches the link, applies
# the database migrations and starts the service again. A rollback is pointing the link back
# after restoring the backup taken before the upgrade (see docs/install/native.md).
set -euo pipefail
# Code under /opt and the unit must never be writable by anyone but root, whatever the caller's
# umask is (some shells and container tools use 0000).
umask 0022

# Branding: the only place this script names the app (see AGENTS.md "Branding").
app=assetboard
env_prefix=ASSETBOARD
repo_url=https://github.com/LexanderPL/AssetBoard

version=latest
release_url=
python_bin=python3
root=
start=1

usage() {
  cat <<EOF
Usage: $0 [--version X.Y.Z|latest] [--python PATH] [--release-url URL] [--root DIR] [--no-start]

  --version      Release to install (default: latest).
  --python       Python 3.13+ interpreter used for the virtual environment (default: python3).
  --release-url  Directory holding the release files, instead of the GitHub release
                 (https:// or file://, for mirrors, a build from a git checkout, and testing).
  --root DIR     Install under DIR without creating users or touching systemd (for testing).
  --no-start     Install and enable the service but leave it stopped.
EOF
}

while (($#)); do
  case $1 in
    --version) version=${2:?--version needs a value}; shift 2 ;;
    --python) python_bin=${2:?--python needs a value}; shift 2 ;;
    --release-url) release_url=${2:?--release-url needs a value}; shift 2 ;;
    --root) root=${2:?--root needs a value}; shift 2 ;;
    --no-start) start=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ -n $root ]]; then
  mkdir -p "$root"
  root=$(cd "$root" && pwd)
fi
prefix=$root/opt/$app
config_dir=$root/etc/$app
env_file=$config_dir/$app.env
# The unit sets the same data directory (Environment=${env_prefix}_DATA_DIR).
data_dir=$root/var/lib/$app
backup_dir=$root/var/backups/$app
unit_path=$root/etc/systemd/system/$app.service

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Runs a command as the service user (as the current user with --root), with umask 077 so that
# the files it creates are readable by that user only.
as_app() {
  if [[ -n $root ]]; then (umask 077 && "$@"); else (umask 077 && runuser -u "$app" -- "$@"); fi
}

# Runs an app command (for example `migrate`) with the service's user, settings and data
# directory, outside the service's sandbox. With --root, only the data directory is set.
run_app_command() {
  if [[ -n $root ]]; then
    env "${env_prefix}_ENV=prod" "${env_prefix}_DATA_DIR=$data_dir" "$prefix/current/bin/$app" "$@"
  else
    systemd-run --quiet --wait --pipe --collect --service-type=exec \
      -p User="$app" -p Group="$app" -p UMask=0077 \
      -p EnvironmentFile="$env_file" \
      -E "${env_prefix}_ENV=prod" -E "${env_prefix}_DATA_DIR=$data_dir" \
      "$prefix/current/bin/$app" "$@"
  fi
}

service_active() {
  [[ -z $root ]] && systemctl is-active --quiet "$app.service"
}

if [[ -z $root && $EUID -ne 0 ]]; then
  die "run as root (or with sudo)"
fi

step "Checking prerequisites"
if [[ -z $root ]]; then
  command -v systemctl >/dev/null || die "systemd is required"
  if command -v apt-get >/dev/null; then
    missing=()
    for pkg in python3 python3-venv curl ca-certificates; do
      dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'ok installed' || missing+=("$pkg")
    done
    if ((${#missing[@]})); then
      echo "Installing ${missing[*]}"
      apt-get update
      apt-get install --no-install-recommends -y "${missing[@]}"
    fi
  fi
fi
command -v curl >/dev/null || die "curl is required"
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v "$python_bin" >/dev/null || die "Python not found: $python_bin"
if ! "$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
  die "$python_bin is $("$python_bin" -V 2>&1); Python 3.13 or later is required.
See docs/install/native.md (older distributions) for installing it with uv."
fi
"$python_bin" -c 'import ensurepip, venv' 2>/dev/null ||
  die "$python_bin cannot create virtual environments (on Debian/Ubuntu: apt install python3-venv)"
python_real=$(readlink -f "$(command -v "$python_bin")")
if [[ -n $(find "$python_real" "$(dirname "$python_real")" -maxdepth 0 -perm /022 2>/dev/null) ]]; then
  die "$python_real or its directory is writable by other users; the service would run code
anyone could change. Fix the permissions (for example: chmod -R go-w $(dirname "$(dirname "$python_real")"))"
fi
echo "Using $("$python_bin" -V) at $(command -v "$python_bin")"

if [[ $version == latest ]]; then
  [[ -z $release_url ]] || die "--release-url needs an explicit --version"
  step "Finding the latest release"
  latest=$(curl -fsSLI --proto '=https' -o /dev/null -w '%{url_effective}' "$repo_url/releases/latest")
  version=${latest##*/v}
fi
version=${version#v}
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || die "not a release version: $version"
echo "Version: $version"
release_url=${release_url:-$repo_url/releases/download/v$version}
release_url=${release_url%/}

wheel=$app-$version-py3-none-any.whl
requirements=$app-$version-requirements.txt
files=("$wheel" "$requirements" "$app.service" "$app.env.example")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

step "Downloading release files from $release_url"
for file in SHA256SUMS "${files[@]}"; do
  echo "  $file"
  curl -fsSL --proto '=https,file' -o "$work/$file" "$release_url/$file"
done

step "Verifying checksums"
for file in "${files[@]}"; do
  grep -E "^[0-9a-f]{64}  $file\$" "$work/SHA256SUMS" >>"$work/expected" ||
    die "$file is not listed in SHA256SUMS"
done
(cd "$work" && sha256sum --check --strict expected)

if [[ -z $root ]] && ! id -u "$app" >/dev/null 2>&1; then
  step "Creating system user $app"
  useradd --system --user-group --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$app"
fi

release_dir=$prefix/releases/$version
if [[ -f $release_dir/.installed ]]; then
  step "Release $version is already installed in $release_dir"
else
  step "Creating the virtual environment in $release_dir"
  # A directory without the .installed marker is a failed earlier attempt: start again.
  rm -rf "$release_dir"
  mkdir -p "$prefix/releases"
  chmod 0755 "$prefix" "$prefix/releases"
  "$python_bin" -m venv "$release_dir"
  pip=("$release_dir/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir)
  # Exact dependency versions and hashes, as locked for this release; wheels only, so nothing
  # is compiled on this machine.
  "${pip[@]}" --require-hashes --only-binary=:all: --no-deps -r "$work/$requirements"
  "${pip[@]}" --no-deps "$work/$wheel"
  "$release_dir/bin/$app" --help >/dev/null || die "the installed release does not start"
  touch "$release_dir/.installed"
fi

step "Writing configuration"
mkdir -p "$config_dir"
chmod 0755 "$config_dir"
if [[ -e $env_file ]]; then
  echo "Keeping existing $env_file"
else
  install -m 0640 "$work/$app.env.example" "$env_file"
  echo "Created $env_file: set ${env_prefix}_BASE_URL and ${env_prefix}_TRUSTED_PROXIES there"
fi
chmod 0640 "$env_file"
# Checked before anything is stopped: the app refuses to start with this removed variable true.
if grep -Eiq "^[[:space:]]*${env_prefix}_ALLOW_REGISTRATION=[\"']?(1|true|t|yes|y|on)[\"']?[[:space:]]*\$" "$env_file"; then
  die "${env_prefix}_ALLOW_REGISTRATION was removed. Delete that line from $env_file and run this
again; open registration is now turned on in the app (Instance settings) and needs SMTP."
elif grep -Eq "^[[:space:]]*${env_prefix}_ALLOW_REGISTRATION=" "$env_file"; then
  warn "${env_prefix}_ALLOW_REGISTRATION was removed and is ignored; delete it from $env_file"
fi
mkdir -p "$data_dir" "$backup_dir"
chmod 0700 "$data_dir" "$backup_dir"
if [[ -z $root ]]; then
  chown "root:$app" "$env_file"
  chown "$app:$app" "$data_dir" "$backup_dir"
fi

step "Checking the secret key"
# The key encrypts two-factor secrets and signs tokens: it is created once, never replaced,
# readable by the service user only, and backed up with the database.
secret_file=$data_dir/secret_key
if grep -Eq "^[[:space:]]*${env_prefix}_SECRET_KEY=." "$env_file"; then
  echo "${env_prefix}_SECRET_KEY is set in $env_file; back it up with the database"
elif [[ -s $secret_file ]]; then
  echo "Keeping existing $secret_file"
else
  as_app "$release_dir/bin/python" -c '
import os, secrets, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write(secrets.token_urlsafe(32))
    stream.flush()
    os.fsync(stream.fileno())
' "$secret_file"
  echo "Created $secret_file"
fi
if [[ -e $secret_file ]]; then
  chmod 0600 "$secret_file"
  [[ -n $root ]] || chown "$app:$app" "$secret_file"
fi

previous=$(readlink "$prefix/current" 2>/dev/null || true)
if [[ $previous == "$release_dir" ]]; then
  step "Release $version is already active"
  upgrade=0
else
  upgrade=1
fi

was_active=0
if ((upgrade)) && service_active; then
  step "Stopping $app.service for the upgrade"
  systemctl stop "$app.service"
  was_active=1
fi

database=$data_dir/$app.db
if ((upgrade)) && [[ -f $database ]]; then
  backup=$backup_dir/$app-$(date -u +%Y%m%dT%H%M%SZ)-before-$version.db
  step "Backing up the database to $backup"
  # The SQLite backup API gives a consistent copy even with the WAL. It runs as the service user
  # so that SQLite never leaves root-owned -wal/-shm files behind.
  as_app "$release_dir/bin/python" -c '
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
target.close()
source.close()
' "$database" "$backup"
  if [[ -f $secret_file ]]; then
    as_app cp -p "$secret_file" "$backup.secret_key"
  fi
  echo "Copy $backup_dir to another machine as part of your regular backups."
fi

if ((upgrade)); then
  step "Activating release $version"
  ln -sfn "$release_dir" "$prefix/current.new"
  mv -T "$prefix/current.new" "$prefix/current"
  [[ -z $previous ]] || echo "Previous release: $previous"
fi

if ((upgrade)); then
  step "Applying database migrations"
fi
if ((upgrade)) && ! run_app_command migrate; then
  if ((upgrade)) && [[ -n $previous ]]; then
    ln -sfn "$previous" "$prefix/current.new"
    mv -T "$prefix/current.new" "$prefix/current"
    warn "switched back to $previous"
  fi
  die "'$app migrate' failed (see the error above); the service is stopped. Fix the cause and
run this again, or start the previous release with: systemctl start $app
If the database was changed, restore the backup in $backup_dir first
(docs/install/native.md, Rolling back)."
fi

step "Installing the systemd unit $unit_path"
mkdir -p "$(dirname "$unit_path")"
unit_changed=0
cmp -s "$work/$app.service" "$unit_path" || unit_changed=1
install -m 0644 "$work/$app.service" "$unit_path"
if [[ -n $root ]]; then
  echo "Skipping systemctl (--root)"
else
  systemctl daemon-reload
  systemctl enable --quiet "$app.service"
  if ((start)); then
    if ((upgrade || unit_changed)) || ! service_active; then
      systemctl restart "$app.service"
    else
      echo "Already running; after changing $env_file run: systemctl restart $app"
    fi
    step "Waiting for the app to become ready"
    ready=0
    for _ in $(seq 30); do
      if curl -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
        ready=1
        break
      fi
      sleep 1
    done
    if ((ready)); then
      echo "Ready on http://127.0.0.1:8000"
    else
      warn "not ready yet; check: journalctl -u $app -n 50"
    fi
  elif ((was_active)); then
    echo "Left $app.service stopped (--no-start); start it with: systemctl start $app"
  fi
fi

step "Done"
cat <<EOF
Release $version is active ($prefix/current).
- Settings: $env_file (then: systemctl restart $app)
- First run: open the app and use the setup token from: journalctl -u $app | grep "setup token"
- Logs: journalctl -u $app -f
- Back up $data_dir/$app.db and $secret_file together; see docs/deployment.md#backups.
Old releases stay in $prefix/releases; remove the ones you no longer need.
EOF
