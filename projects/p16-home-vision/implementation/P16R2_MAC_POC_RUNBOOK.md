# P16R2 Mac mini PoC Runbook

Status: code-level acceptance passed. This runbook is for Human local product acceptance before the one-time OMV freeze/migration.

## 1. Scope

This PoC runs only the new P16R2 media/UI path:

```text
RAW (read-only input)
→ Web MP4 (HEVC stream-copy + hvc1, AAC audio)
→ 1fps working frames + PTS manifest
→ 16 kHz mono WAV for future local ASR
→ status.json
→ /api/r2/assets
→ React/TanStack Query UI
```

It does **not**:

- delete RAW;
- connect P20/Tokyo/Codex;
- run long-term memory or AI analysis;
- freeze or modify OMV;
- require NVR credentials for the first smoke test.

## 2. Mac mini prerequisites

- Python 3.12+
- ffmpeg + ffprobe
- Node.js 22+
- repository branch `feature/p16r2-mac-poc`

Keep runtime secrets out of Git. NVR IP/account/password are intentionally deferred until the later live-resource step.

## 3. Install backend

```bash
cd implementation
python3.12 -m venv .venv-r2
source .venv-r2/bin/activate
pip install -e '.[dev]'
```

Set a local PoC asset root:

```bash
export HOME_VISION_R2_ASSET_ROOT="$HOME/P16R2/assets"
mkdir -p "$HOME_VISION_R2_ASSET_ROOT"
```

Start API:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Verify:

```bash
curl http://127.0.0.1:8000/api/r2/assets
```

Expected before any asset is processed:

```json
{"items":[]}
```

## 4. Create a no-NVR smoke asset

In a second shell, activate the same venv and export the same asset root:

```bash
cd implementation
source .venv-r2/bin/activate
export HOME_VISION_R2_ASSET_ROOT="$HOME/P16R2/assets"
bash scripts/p16r2_make_fixture.sh
```

Expected:

- generated RAW remains under `$HOME/P16R2/fixtures/`;
- derived asset appears under `$HOME/P16R2/assets/<asset-id>/`;
- `web/video.mp4` exists;
- `frames/manifest.jsonl` exists;
- `audio/speech.wav` exists;
- `status.json` reports `state=derived` and `raw_deleted=false`.

## 5. Start the new frontend

```bash
cd implementation/frontend-r2
npm install
npm run dev
```

Open the Vite URL (default `http://127.0.0.1:5173`). The dev server proxies `/api` to `127.0.0.1:8000`.

Human acceptance checks:

1. the smoke asset appears without full-page reload;
2. expanding one asset changes only that asset card;
3. Web Media playback works;
4. stage states are visible per asset;
5. RAW delete is disabled and no delete API exists;
6. switching `仅处理中 / 全部` is local UI state only;
7. API polling does not disturb playback/expanded state.

## 6. First real recording test (later)

Only after the Human accepts the PoC shell, choose one bounded real NVR recording and supply runtime-only NVR connection details. Do not commit credentials.

The first live-media test should validate:

- source codec/container/audio probe;
- HEVC video stream-copy into MP4;
- AAC playback audio;
- 1fps PTS/absolute-time mapping;
- duration/seek behavior in Safari/Chrome;
- Mac mini CPU/RAM/disk pressure;
- working-frame growth rate.

## 7. OMV migration comes after Mac acceptance

Do not freeze OMV before this PoC passes Human local acceptance.

After acceptance:

```text
freeze old OMV workers
→ capture DB/filesystem static point
→ inventory already-downloaded assets
→ checksum/sync to Mac mini
→ import/reconcile state
→ download only missing NVR recordings
```

No OMV media should be re-downloaded merely because runtime moved to Mac mini.
