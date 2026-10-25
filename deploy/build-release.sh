#!/usr/bin/env bash
# Build the native-install release files from a git checkout into OUTDIR.
# Docs: docs/install/native.md ("From a git checkout"). The release workflow runs it too, so a
# local build and a GitHub release hold the same files:
#
#   <app>-<version>-py3-none-any.whl     the app (compiled .mo catalogs included)
#   <app>-<version>-requirements.txt     locked runtime dependencies, with hashes
#   <app>.service, <app>.env.example     systemd unit and settings template
#   install-debian.sh, SHA256SUMS
#
# Needs uv (https://docs.astral.sh/uv/). Run it as a normal user, not as root. Then install with:
#   sudo deploy/install-debian.sh --version <version> --release-url file://<OUTDIR>
set -euo pipefail

# Branding: the only place this script names the app (see AGENTS.md "Branding").
app=assetboard

out=${1:?usage: $0 OUTDIR}
here=$(cd "$(dirname "$0")" && pwd)
checkout=$(cd "$here/.." && pwd)

command -v uv >/dev/null || { echo "error: uv is required (see docs/install/native.md)" >&2; exit 1; }

version=$(cd "$checkout" && uv version --short)
mkdir -p "$out"
out=$(cd "$out" && pwd)
wheel=$app-$version-py3-none-any.whl
requirements=$app-$version-requirements.txt

printf '==> Building %s %s into %s\n' "$app" "$version" "$out"
rm -f "$out/$wheel"
# The wheel's build hook (hatch_build.py) compiles the translation catalogs; app.css is committed.
(cd "$checkout" && uv build --wheel --out-dir "$out")
[[ -f $out/$wheel ]] || { echo "error: uv build did not produce $wheel" >&2; exit 1; }
(cd "$checkout" && uv run --no-project python scripts/verify_wheel.py "$out/$wheel")

printf '==> Exporting locked runtime requirements\n'
(cd "$checkout" && uv export --frozen --no-default-groups --no-emit-project \
  --format requirements-txt --output-file "$out/$requirements" >/dev/null)

install -m 0644 "$here/systemd/$app.service" "$out/$app.service"
install -m 0644 "$here/$app.env.example" "$out/$app.env.example"
install -m 0755 "$here/install-debian.sh" "$out/install-debian.sh"

(cd "$out" && sha256sum "$wheel" "$requirements" "$app.service" "$app.env.example" \
  install-debian.sh >SHA256SUMS)

printf '==> Done\n'
ls -l "$out"
printf '\nInstall with:\n  sudo %s/install-debian.sh --version %s --release-url file://%s\n' \
  "$out" "$version" "$out"
