# P16 implementation

P16R1 legacy implementation remains under `app/` and existing workers.

P16R2 Mac PoC adds two isolated entrypoints:

- `p16-media-derive`: read one RAW recording and derive Web MP4 + 1fps frames + 16 kHz ASR WAV without deleting RAW. An explicitly configured local `--local-asr-command` can consume VAD regions and return timestamped JSON transcript segments; without it transcript state is `not_configured`.
- `media_derivation.local_speech`: dependency-free mono PCM16 energy VAD and a subprocess-only local ASR adapter. It does not contact a provider or download models.
- `app.domain.frame_reduce_benchmark`: reports reference-time recall, reduction ratio, and runtime; benchmark references must be human reviewed before thresholds are selected.
- `app.domain.analysis_package`: content-addressed artifact manifest and atomic immutable package publication.
- `frontend-r2/`: React + TanStack Query asset workbench consuming `/api/r2/assets`.

PoC runtime state is rooted at `HOME_VISION_R2_ASSET_ROOT` (default `/state/r2-assets`).

The PoC does not call P20, Tokyo, Codex, or any remote AI workflow, and it exposes no RAW deletion API. Synthetic CI fixtures validate mechanics only; no representative household-media benchmark or real local-ASR quality acceptance is claimed.
