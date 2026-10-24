# P20 Transfer Hub 0.5.5

P20 is a deterministic transfer control plane for the Tokyo VPS. Runtime AI, Sidechat, Codex and automatic repair loops are not part of the 0.5.x line.

0.5.5 is the production Receiver hotfix over 0.5.4: QQ CDN range reads no longer trust inconsistent Last-Modified/ETag validators, Browser Receiver uses ticket-sized Range I/O without a redundant full GET, and Receiver URL generation refreshes the QQ runtime idle lease with public-share fallback on runtime transport races.

0.5.4 was the production self-audit patch: WebUI uses the Tailnet boundary without a second token login, health probes no longer defeat deep sleep, Browser Resolver/public URL validation rejects non-global destinations, and runtime control credentials are not placed in process argv.

The execution model is:

```text
Resolve -> Fetch -> Process -> Deliver -> Receive -> ACK/Cleanup
```

## Core architecture

- FastAPI REST API + OpenAPI is the authoritative public contract.
- SQLite WAL `transfer.db` is the only job/stage queue truth.
- `job_stages` uses atomic claims, leases, heartbeat and fenced claim tokens.
- `artifacts` persists stage outputs, including multi-file and split archive output.
- `delivery_attempts` persists provider task identity for upload reconciliation.
- Fetch / Process / Deliver have independent concurrency limits.
- Default concurrency is Fetch=2, Process=1, Deliver=1.

A slow QQ upload does not occupy a Fetch slot. A Process retry reuses completed Fetch artifacts instead of downloading them again.

## Fetch engines

Fetch engines are accessed through a common adapter registry. aria2 uses one shared on-demand JSON-RPC daemon with persisted session/GID state; yt-dlp, gallery-dl, N_m3u8DL-RE and UAV use subprocess adapters.

aria2 pause/resume/cancel is implemented through its RPC task identity rather than process signals. The daemon starts only when an aria2 Fetch needs it and exits with the Hub.

Browser Resolver is a deterministic Playwright network resolver. It is not AI. The host runtime agent starts its container only around active browser-resolution requests and stops it after the final caller releases it.

## Process and delivery

Process owns verification, safe archive extraction, packaging and encryption. Desktop delivery produces AES-256 ZIP by policy; iPhone/Android profiles are prepared as phone-ready plaintext artifacts.

QQ Runtime is required during QQ Flash Deliver and may also be woken briefly during Receive to mint a fresh temporary CDN URL from a persisted `fileSetId`. QQ Flash provider task IDs and idempotency keys are persisted so Hub restarts can reconnect to an existing upload instead of blindly creating another share.

QQ Runtime and Browser Resolver are host-controlled, loopback-only lifecycle services. Neither is required simply to open the P20 UI or fetch a normal file.

## Receiver

Receiver ticket schema is `p20.receiver-ticket/v2`. It carries QQ Flash artifact metadata including stable `fileSetId`, an authenticated resolver URL, public-share fallback metadata, post-processing instructions and the ACK endpoint. Temporary CDN URLs are minted on demand and are not treated as durable state.

`p20-receive` 0.3.0 is the reliability-first CLI. It keeps Range resume and SHA-256 verification, asks the Hub resolver for a native QQNT URL first, and follows structured `WAITING_FOR_QQ_SHARE` backoff when only the public share layer is available. The CLI reports the reason and next retry on stderr, defaults to a 600-second bounded wait, then optionally calls local 7-Zip/7zz for decrypt/extract. ACK happens only after requested post-processing succeeds.

The Web UI exposes Browser Receiver as the primary READY action. ZIP AES extraction uses zip.js and downloads directly from the QQ CDN; bulk file bytes are not proxied through Tokyo. Manual share-link/password copy remains an advanced fallback.

## Idle behaviour

The candidate deployment binds Hub internally at `127.0.0.1:17614`. A systemd socket remains at the stable Tailnet endpoint `100.84.55.11:17610` and wakes the Hub through `systemd-socket-proxyd`.

When there are no READY/RUNNING/PAUSED stages and the activity window expires, Hub may return to deep sleep. QQ Runtime has its own short idle policy and may be started for Deliver or temporary-URL resolution during Receive. Browser Resolver is request-scoped. Processing tools are one-shot processes.

See `../docs/P20_0_5_0_TRANSFER_PIPELINE.md`, `../docs/API.md`, `../docs/MCP.md` and `../docs/P20_RECEIVER.md` for the contracts and deployment acceptance criteria.
