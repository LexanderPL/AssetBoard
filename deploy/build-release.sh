#!/usr/bin/env bash
# Build the native-install release files from a git checkout into OUTDIR.
# Docs: docs/install/native.md ("From a git checkout"). The release workflow runs it too, so a
# local build and a GitHub release hold the same files:
#
#   <app>-<version>-py3-none-any.whl     the app (compiled .mo catalogs included)
#   <app>-<version>-requirements.txt     locked runtime dependencies, with hashes
#   <app>-<version>-sbom.cdx.json        CycloneDX SBOM for those dependencies
#   THIRD_PARTY_LICENSES                 aggregate third-party notices
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
requirements=$app-$version-requirements.txt

printf '==> Building %s %s into %s\n' "$app" "$version" "$out"
rm -f "$out/$app"-*-py3-none-any.whl
# The wheel's build hook (hatch_build.py) compiles the translation catalogs; app.css is committed.
(cd "$checkout" && uv build --wheel --out-dir "$out")
# The wheel's file name carries the PEP 440 form of the version (0.4.0-rc.1 becomes 0.4.0rc1),
# so it is found rather than derived; install-debian.sh reads it from SHA256SUMS.
wheels=("$out/$app"-*-py3-none-any.whl)
[[ ${#wheels[@]} -eq 1 && -f ${wheels[0]} ]] || { echo "error: uv build did not produce one wheel" >&2; exit 1; }
wheel=$(basename "${wheels[0]}")
(cd "$checkout" && uv run --no-project python scripts/verify_wheel.py "$out/$wheel")

printf '==> Exporting locked runtime requirements\n'
(cd "$checkout" && uv export --frozen --no-default-groups --no-emit-project \
  --format requirements-txt --output-file "$out/$requirements" >/dev/null)

printf '==> Generating CycloneDX SBOM\n'
(cd "$checkout" && uvx --from cyclonedx-bom==8.0.1 cyclonedx-py requirements \
  "$out/$requirements" --pyproject pyproject.toml --output-format JSON \
  --output-file "$out/$app-$version-sbom.cdx.json" >/dev/null)
(cd "$checkout" && uv run --no-project python - \
  "$out/$app-$version-sbom.cdx.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as sbom_file:
    sbom = json.load(sbom_file)
assert sbom.get("bomFormat") == "CycloneDX", sbom.get("bomFormat")
assert sbom.get("components"), "the SBOM has no components"
PY
)

install -m 0644 "$here/systemd/$app.service" "$out/$app.service"
install -m 0644 "$here/$app.env.example" "$out/$app.env.example"
install -m 0644 "$checkout/THIRD_PARTY_LICENSES" "$out/THIRD_PARTY_LICENSES"
install -m 0755 "$here/install-debian.sh" "$out/install-debian.sh"

built=$(printf '%s\n' "$wheel" "$requirements" "$app-$version-sbom.cdx.json" \
  "$app.service" "$app.env.example" THIRD_PARTY_LICENSES install-debian.sh)
(cd "$out" && printf '%s\n' "$built" | xargs sha256sum >SHA256SUMS)
(cd "$out" && sha256sum --check --strict SHA256SUMS >/dev/null)
# Both directions: every generated file is listed and nothing stale or unlisted is shipped.
# uv writes a .gitignore into its output directory; it is not a release file.
listed=$(awk '{print $2}' "$out/SHA256SUMS" | sed 's/^\*//' | sort)
actual=$(find "$out" -maxdepth 1 -type f ! -name SHA256SUMS ! -name .gitignore \
  -printf '%f\n' | sort)
if [[ $listed != "$(printf '%s\n' "$built" | sort)" || $actual != "$listed" ]]; then
  echo "error: SHA256SUMS does not match the files in $out" >&2
  diff <(printf '%s\n' "$listed") <(printf '%s\n' "$actual") >&2 || true
  exit 1
fi

printf '==> Done\n'
ls -l "$out"
printf '\nInstall with:\n  sudo %s/install-debian.sh --version %s --release-url file://%s\n' \
  "$out" "$version" "$out"
