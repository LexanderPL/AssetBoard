# Release checklist — v1.0.0

**Status: prepared, not executed.** This document and the scripts it names do not create a
tag, publish an image, merge a release PR or upload release assets. Those are operator steps,
marked **operator** below. Do not tick a box until the command has been run against the image
and tag named in that step; record the command and result in the
[execution record](#execution-record).

**Release hold (2026-11-20).** The owner's v1.0 hold remains in place: `main` keeps the
pre-alpha README status, the draft release PR #397 is based on an older `main`, and no tag or
image has been published. Do not merge #397, change the README status, tag or publish while the
hold is in place. When the owner lifts it, merge current `main` into #397 (keeping the 1.0
README/changelog), resolve the version files, then run this checklist.

Target: `v1.0.0`, from the previous release tag `v0.8.3`, with the image
`ghcr.io/lexanderpl/assetboard` for `linux/amd64` and `linux/arm64`.

F9.7 (documentation, screenshots and the i18n completeness report) is a required pre-tag step
but is delivered by a separate change. If F9.7 is not merged, stop at step 1. This checklist
does not replace it and does not claim its checks ran.

## 0. Before the release PR

- [ ] F9.9 (phone layout pass) and F9.10 (installable web app) are merged on `main`.
- [ ] F9.7 is merged: user docs, demo-data screenshots and the translation completeness report
      are present. Record the reported Polish coverage; v1.0 requires at least 95% for
      user-facing strings.
- [ ] The working tree is clean and `main` is at the commit to release.
- [ ] `TMPDIR=$(mktemp -d) PYTEST_WORKERS=2 make check` passes.
- [ ] `ASSETBOARD_RUN_BROWSER_TESTS=1 TMPDIR=$(mktemp -d) \
      uv run pytest tests/browser -q -n 1 --no-cov` passes.
- [ ] The Security workflow is green: the pip-audit high/critical gate, the third-party notice
      freshness check and the weekly image scan have no unresolved finding. A known,
      unfixed base-image advisory may stay visible without blocking; record it in the release
      notes if it is still present.
- [ ] `CHANGELOG.md` has been read. The release PR's notes are user-facing: remove internal
      churn, keep breaking changes and required actions, and link the upgrade section.

## 1. Release PR and the 1.0 version

Release Please is configured with `bump-patch-for-minor-pre-major`, so ordinary `feat` commits
after 0.8.3 produce a 0.8.x release. To make the milestone release:

- [ ] The Release Please PR says **1.0.0** in `pyproject.toml`, `.release-please-manifest.json`,
      `uv.lock`, `docs/deployment.md` and `deploy/podman/assetboard.container`.
- [ ] If it proposes 0.8.x instead, land a conventional commit on `main` with the footer
      `Release-As: 1.0.0` (for example `chore: release 1.0.0` plus that footer). Let Release
      Please rebuild the PR; do not hand-edit its generated version files.
- [ ] Review the generated `CHANGELOG.md` entry and the release-please PR diff.
- [ ] Confirm `release-please-config.json` still updates all five version locations
      (`pyproject.toml`, `.release-please-manifest.json`, `uv.lock`, `docs/deployment.md`,
      `deploy/podman/assetboard.container`) and that `deploy/build-release.sh` still builds the
      wheel, requirements, CycloneDX SBOM, `THIRD_PARTY_LICENSES`, systemd unit, env example,
      install script and a `SHA256SUMS` that lists exactly those files.

## 2. Candidate checks before the tag

Build the exact release-PR commit locally; this is the safe rehearsal and never pulls a real
data directory into the test.

```bash
export VERSION=1.0.0
export IMAGE=ghcr.io/lexanderpl/assetboard
export PREVIOUS_TAG=v0.8.3
git checkout <release-pr-merge-commit>
docker build --build-arg VERSION=v${VERSION} -t assetboard:release-candidate .
```

- [ ] Fresh install from the candidate through Compose:

  ```bash
  scripts/release_checks.sh fresh-install assetboard:release-candidate
  ```

  The script starts the image with `compose.example.yaml`, completes first-run setup over HTTP,
  checks `/healthz`, `/readyz`, `/setup`, the signed-in home page and the F9.10 manifest,
  service worker and icons, then removes its disposable project and volume.

- [ ] Upgrade a populated database from the previous release:

  ```bash
  scripts/release_checks.sh upgrade "$IMAGE:${PREVIOUS_TAG#v}" assetboard:release-candidate
  ```

  The previous image creates an administrator and the deterministic demo workspace, then the
  candidate opens the same disposable volume. The script signs in, compares key business-table
  row counts, a content digest of the financial tables and the Alembic revision.

- [ ] Backup/restore round trip:

  ```bash
  scripts/release_checks.sh backup-restore assetboard:release-candidate
  ```

  It takes a consistent online SQLite backup plus `secret_key`, restores them with
  `assetboard restore` into a clean volume, signs in with the original account and compares the
  data and key hash.

- [ ] Forward-only migrations on a copy of demo data. Use an older tag whose schema predates the
      newest migration, not the immediately previous tag when both already share a revision:

  ```bash
  scripts/release_checks.sh upgrade "$IMAGE:0.8.2" assetboard:release-candidate \
    --require-migration
  ```

  `--require-migration` fails if the revisions are equal. When they differ, the script also
  starts the older image against the upgraded copy, requires `schema_newer` and verifies the
  revision and the financial row counts and contents did not change.

- [ ] The Docker metadata tag policy is unchanged and produces the expected tags for
      `v1.0.0`:

  ```bash
  scripts/verify_release_tags.py --tag v1.0.0 --print-expected
  # 1.0.0
  # 1.0
  # 1
  # latest
  ```

  The tag step in `.github/workflows/docker.yml` checks the actual
  `docker/metadata-action` output with the same helper before it pushes, so a dropped or extra
  tag fails the build. Stable 0.x tags still omit the major-only tag.

- [ ] **Operator:** decide the GitHub release notes now: upgrade notes, required actions,
      backup reminder, known advisories, digest and the statement that downgrading requires a
      restored backup.

## 3. Tag and publish (operator only)

This is the only step that creates the release. It is deliberately outside this change.

- [ ] **Operator:** merge the Release Please PR.
- [ ] **Operator:** Release Please creates tag `v1.0.0` and the GitHub release.
- [ ] **Operator:** the Docker workflow publishes the multi-arch image and the Release Please
      workflow attaches the native-install files. If either job fails, do not announce the
      release; fix forward with a patch release (or, only with release-manager approval, delete
      the broken release and tag and retry).

## 4. Fresh install from the published image

Run after the tag publishes. The automatic `release-checks` job in `docker.yml` runs a fresh
install, the previous-release upgrade, the older-tag migration check, the backup/restore round
trip and the tag/manifest checks; the commands are also here so an operator can repeat and
record them.

- [ ] Pull the exact version tag and run the same fresh-install check:

  ```bash
  docker pull "$IMAGE:$VERSION"
  scripts/release_checks.sh fresh-install "$IMAGE:$VERSION"
  ```

- [ ] Confirm the published image carries the version label, not a local build:

  ```bash
  docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' \
    "$IMAGE:$VERSION"
  ```

  It reports `1.0.0`.

## 5. Upgrade from the previous tag with a populated database

- [ ] Upgrade the previous release's published image with its real tag:

  ```bash
  scripts/release_checks.sh upgrade "$IMAGE:${PREVIOUS_TAG#v}" "$IMAGE:$VERSION"
  ```

- [ ] Record the before/after Alembic revisions printed by the script. If they are equal,
      record that no migration was pending between 0.8.3 and 1.0.0; the forward-only check in
      step 6 still exercises the migration path with an older tag.
- [ ] The script signs in with the original account after the upgrade and compares the data
      rows; it removes the container on exit. To click through the pages yourself, rerun with
      `--keep`:

  ```bash
  scripts/release_checks.sh upgrade "$IMAGE:${PREVIOUS_TAG#v}" "$IMAGE:$VERSION" --keep
  ```

  Open the URL and sign in as the account the first command created; confirm the demo
  workspace and the Home, reports, loans and FIRE pages. Remove the printed container,
  volume and Compose project afterwards — they contain disposable test data.

## 6. Migrations forward-only on a copy of demo data

- [ ] Run the migration rehearsal with an older tag that predates the newest migration:

  ```bash
  scripts/release_checks.sh upgrade "$IMAGE:0.8.2" "$IMAGE:$VERSION" --require-migration
  ```

- [ ] The script must print the applied revision range, `schema_newer` from the older image,
      and unchanged financial rows/contents and revision after the older image is stopped.
- [ ] No command in this checklist runs `alembic downgrade` against a real database. A
      downgrade is not a supported release path; roll back by restoring the backup taken before
      the upgrade.

## 7. Multi-arch image and tag verification

- [ ] Verify that `1.0.0`, `1.0`, `1`, and `latest` exist, resolve to the same manifest digest
      and both carry `linux/amd64` and `linux/arm64`:

  ```bash
  scripts/release_checks.sh verify-tags "$IMAGE" v1.0.0
  ```

- [ ] Inspect the manifest list once by hand and record the digest in the release notes:

  ```bash
  docker buildx imagetools inspect "$IMAGE:$VERSION"
  ```

- [ ] The prerelease rule is unchanged: `v1.0.0-rc.1` would publish only `1.0.0-rc.1` and must
      not move `latest`. Stable 0.x tags never publish a major-only tag.

## 8. Security scan, SBOM and release assets

- [ ] The tag's Docker workflow passed the fixed HIGH/CRITICAL Trivy gate on both the amd64 and
      native arm64 images. The non-blocking full report may still show unfixed advisories;
      record them in the release notes if they affect users.
- [ ] The Security workflow passed the pip-audit high/critical gate and
      `make licenses-check` on the release commit.
- [ ] The published manifest carries the BuildKit attestation. This command must find at least
      one `attestation-manifest` entry:

  ```bash
  docker buildx imagetools inspect "$IMAGE:$VERSION" --format '{{json .}}' \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); ms=d["manifest"]["manifests"]; assert any((m.get("annotations") or {}).get("vnd.docker.reference.type") == "attestation-manifest" for m in ms), "no SBOM/provenance attestation"'
  ```

- [ ] The GitHub release has every file produced by `deploy/build-release.sh`, including the
      wheel, hash-locked requirements, CycloneDX `*-sbom.cdx.json`, `THIRD_PARTY_LICENSES`,
      `SHA256SUMS`, the systemd unit, env example and install script. The release workflow
      verifies the upload; confirm it in the job log.
- [ ] Download the assets to a scratch directory and verify them independently:

  ```bash
  gh release download "v$VERSION" --dir "/tmp/assetboard-v$VERSION" --clobber
  (cd "/tmp/assetboard-v$VERSION" && sha256sum --check --strict SHA256SUMS)
  python3 -c 'import json,glob; [json.load(open(p)) for p in glob.glob("/tmp/assetboard-v$VERSION/*-sbom.cdx.json")]'
  ```

- [ ] The release files are attached to the release for `v1.0.0`, not to an older tag.

## 9. Notes, documentation, screenshots and i18n

- [ ] The GitHub release body contains upgrade notes: back up the database and `secret_key`
      before upgrading, migrations run at startup, `/readyz` reports when they finish, a
      database written by 1.0 cannot be used by a release with an older schema (0.8.2 and
      earlier; 0.8.3 shares the 1.0 revision), and rollback means restoring the backup.
- [ ] `docs/deployment.md` has the 1.0 upgrade notes and the image-tag rule. `README.md` and the
      install pages point at the current release.
- [ ] F9.7's screenshot script has been run against the demo data in light/dark and
      desktop/mobile, and the committed images match the script output.
- [ ] F9.7's translation report shows Polish at or above 95% for user-facing strings, with no
      empty or fuzzy entries for strings that ship in v1.0. The catalogs compile
      (`make i18n-compile`) and the CI freshness check passes.
- [ ] The demo builder still seeds every module (including loans and FIRE) and
      `assetboard demo-seed` remains runnable in the release image.

## 10. Final record and hand-off

- [ ] Fill the execution record below with the exact commands, image digest, tag and date.
- [ ] **Operator:** confirm the GitHub release is published, the package visibility allows the
      intended users to pull it, and the release notes link to this checklist.
- [ ] **Operator:** announce the release. If any post-publish check failed, do not announce;
      record the failure and fix forward.

## Execution record

Do not mark a row passed unless the command was run against the named image/tag. "N/A" needs a
reason.

| # | Check | Command or evidence | Result | Date / operator |
| --- | --- | --- | --- | --- |
| 0 | Unit, browser and security checks | `make check`, browser pytest, Security workflow |  |  |
| 1 | Release PR is 1.0.0 | PR link, version files diff |  |  |
| 2 | Candidate fresh install | `release_checks.sh fresh-install assetboard:release-candidate` |  |  |
| 2 | Candidate upgrade from v0.8.3 | `release_checks.sh upgrade …` |  |  |
| 2 | Candidate backup/restore | `release_checks.sh backup-restore …` |  |  |
| 2 | Candidate forward-only migration | `release_checks.sh upgrade … --require-migration` |  |  |
| 3 | Tag and publish | Release Please run, Docker workflow run |  |  |
| 4 | Published fresh install | `release_checks.sh fresh-install "$IMAGE:$VERSION"` |  |  |
| 5 | Published upgrade from previous tag | `release_checks.sh upgrade …` |  |  |
| 6 | Published forward-only migration | `release_checks.sh upgrade 0.8.2 … --require-migration` |  |  |
| 7 | Tags and multi-arch manifests | `release_checks.sh verify-tags …`, digest |  |  |
| 8 | Trivy, pip-audit, SBOM, assets | Workflow runs, `sha256sum --check` |  |  |
| 9 | Notes, docs, screenshots, i18n | Links and F9.7 report |  |  |
| 10 | Operator release hand-off | Release URL, package visibility |  |  |

Manifest digest: `sha256:________________`

Release URL: `________________`

## Failure handling

- **A pre-tag check fails.** Fix on `main` and rebuild the release PR; do not tag.
- **The tag build fails before publishing.** Fix forward with a patch release. Do not hand-push
  an image with different contents under `1.0.0`.
- **A post-publish check fails.** Stop the announcement, record the failure here and in the
  release notes, and fix forward. Replacing a published tagged image is not the release
  process.
- **A user needs to roll back.** Stop the app, restore the pre-upgrade database and
  `secret_key`, and run the older release against that restored copy. Never point an older
  release at a database already migrated by 1.0.
