# Home Vision Deployment / Real-Environment Handoff

Baseline: P16 Stage 4 / B — Balanced Dark Glass / branch `fix/p16-recordings-page-performance`.

This runbook prepares the first real OMV/NVR execution. It does not authorize bypassing Issue #94 Access Preflight.

## 1. What is already fixture-validated

- Home Vision FastAPI + SQLite/Alembic runtime;
- NVR discovery/parser/transport boundary;
- archive `.part → validation → ffprobe → SHA-256 → atomic rename` pipeline;
- storage A/B allocator and delete/Tombstone recovery;
- local analysis profile + cheap screening;
- ModelRunner contract / structured result persistence;
- query-only Consumer API;
- Human-approved r11 UI runtime;
- Linux container/Compose packaging.

## 2. Before touching the OMV runtime

Do not delete the legacy `aibox-edge` LXC first.

Required sequence:

```text
inspect aibox-edge unique state/config/credential references
→ migrate anything unique or prove it disposable
→ stop aibox-edge
→ stop OMV VM only when resource reassignment is actually needed
→ reassign CPU/RAM to OMV VM
→ boot OMV VM
→ validate container runtime + state/archive mount paths
→ complete #94
→ only after successful migration/validation, retire/delete aibox-edge
```

## 3. Prepare non-secret paths

Create/identify three directories on the OMV Edge VM:

```text
HOME_VISION_STATE_DIR   # local filesystem, SQLite state only
HOME_VISION_ARCHIVE_A   # archive volume A
HOME_VISION_ARCHIVE_B   # archive volume B
```

`HOME_VISION_STATE_DIR` must not be NFS/SMB/CIFS. A and B must be separate intended archive volumes.

Choose the runtime UID/GID that should own write access to all three paths. Do not put NVR credentials in `.env` or Git.

## 4. Run local storage Preflight

From `implementation/`:

```bash
python3 scripts/preflight_local_storage.py \
  --state-dir "$HOME_VISION_STATE_DIR" \
  --archive-a "$HOME_VISION_ARCHIVE_A" \
  --archive-b "$HOME_VISION_ARCHIVE_B" \
  --require-distinct-devices \
  --json
```

The script uses only dedicated `.home-vision-preflight-*` temporary files and removes them. It does not inspect or mutate existing archive files.

A `BLOCKED` result must be resolved before deployment. Do not downgrade `--require-distinct-devices` on the real OMV host merely to obtain PASS.

## 5. Complete Issue #94

In addition to the storage checks above, #94 requires:

- authenticated Hikvision ISAPI read/search smoke;
- intended container/runtime capability;
- local SQLite state filesystem confirmation;
- separate archive A/B create → fsync → rename → unlink;
- redacted evidence only.

Do not publish credentials, auth headers or raw secret-bearing playback URI.

## 6. Compose validation

Copy `.env.example` to an untracked local `.env` and replace only path/UID/GID values.

Validate and build before start. Release builds must use one shared image; do
not run an unqualified `docker compose build` because every service used to
carry a build stanza and that encouraged repeated image work.

```bash
export P16_RELEASE_REVISION=$(git rev-parse HEAD)
export HOME_VISION_IMAGE=home-vision:${P16_RELEASE_REVISION}
export P16_STATE_ID=omv-a8-home-vision-state-v1
docker compose -f compose.yaml -f compose.release.yaml --profile nvr config >/dev/null
docker compose -f compose.yaml -f compose.release.yaml --profile nvr build --pull=false home-vision
docker compose -f compose.yaml -f compose.release.yaml --profile nvr up -d --no-build --pull never
python scripts/verify_release_consistency.py \
  --expected-revision "$P16_RELEASE_REVISION" --require-all-core
```

Normalization runs only on the MBA using `scripts/m1_normalize_worker.py` with
three fixed worker slots. The worker waits without claiming tasks until its
Finder-mounted `/Volumes/A8_sdb1` archive share is present. The OMV
normalization container is intentionally not part of the release Compose
services. Before each claim, the MBA worker must send the exact
`P16_RELEASE_REVISION` advertised by `/health` and the
`p16-normalize-mba-v1` protocol. The API accepts only `mba-normalize-1` through
`mba-normalize-3` and recordings from 2026-09-01 UTC onward; older recordings
and their lease metadata are excluded.

The Dockerfile installs the locked Python runtime before copying application
source, and BuildKit caches pip downloads. A source-only change therefore
reuses the ffmpeg and dependency layers. No durable external port is defined
by this Compose file. Stable external routing belongs to P13 managed Gateway.

## 7. First start after #94 PASS

```bash
docker compose -f compose.yaml -f compose.release.yaml --profile nvr up -d --no-build --pull never
```

Never use `docker compose down -v`, global prune, or image/volume deletion as
part of a release build. Existing state, archive media, and the thumbnail
disk are outside the build cache and must remain untouched.

The entrypoint runs `alembic upgrade head` before starting FastAPI. Migration failure fails closed and prevents the API from starting.

Then verify from inside the Compose network/runtime boundary as appropriate:

```text
/health
/
/api/v1/capabilities
```

Do not claim production readiness until live W2–W4 NVR/archive smoke tests are executed and recorded.

## 8. Enable real NVR date scanning

The API does not invent dates that have not been queried. After the approved
runtime-only NVR endpoint and credentials are injected, enable the opt-in
`nvr` Compose profile. `scripts/scan_nvr.py` performs bounded read-only
Hikvision history searches and records each day/camera result in `scan_runs`.
The Overview calendar can then distinguish recorded, scanned-without-recording,
and not-yet-scanned days.

Do not place the NVR password in Git, the image, browser storage, logs, or
evidence. A missing credential or failed search keeps the affected date
unknown/failed; it must never be rendered as “无录像”.

## 9. Live evidence still required

After #94 PASS:

1. real NVR discovery/reconciliation smoke;
2. one bounded real archive download + verification;
3. independent A/B storage behavior and delete recovery check using disposable test media only;
4. OMV Edge cheap-screening throughput/resource contention benchmark;
5. later Mac mini/GPU heavy-model benchmark and phase-specific Preflight;
6. P13 managed Gateway integration;
7. Independent Acceptance + Final Product Human Review.

## 10. Stop rule

If source identity, filesystem behavior, permissions, ffmpeg behavior, or runtime resource assumptions differ materially from fixture evidence, stop the affected live step and record the contradiction in GitHub. Do not silently weaken verification or archive safety rules.
