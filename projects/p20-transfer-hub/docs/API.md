# P20 REST API 0.5.7

FastAPI/Pydantic/OpenAPI is the authoritative P20 public contract. Interactive schema is served at `/docs`; machine schema is `/openapi.json`.

## Authentication

The human WebUI has no application-level token login. Tokyo exposes the stable UI/API listener only on the Tailnet socket `100.84.55.11:17610`; that private network boundary is the browser access control for this deployment. The retired `/api/v1/session` cookie exchange was removed in 0.5.4 and those routes return 404.

`GET /health` remains unauthenticated on that private listener. Programmatic callers outside the trusted private-client path use `Authorization: Bearer <P20 API token>`. Tokens are never stored in browser storage or passed through browser UI fields.

The stable Tokyo socket is proxied by user-systemd to the internal Hub on `127.0.0.1:17614`. Docker's internal health probe carries `X-P20-Healthcheck: 1`; that probe deliberately does not refresh the Hub user-activity clock, so health monitoring cannot defeat deep sleep.

For another project consuming P20 as a download channel, use the end-to-end contract in `P20_CHANNEL_USAGE.md`. A job reaching `READY` is only ready for retrieval; the normal automated completion path continues through Receiver Ticket + `p20-receive` + ACK.

## Transfer lifecycle

- `POST /api/v1/jobs` — create URL/source transfer. New jobs use the unified secure delivery policy: AES-256 ZIP + explicit Receiver ACK. Legacy `delivery_profile=desktop|iphone|android` inputs are accepted for compatibility but normalized to `unified`; existing persisted legacy jobs retain their original semantics.
- `GET /api/v1/jobs` — list transfers.
- `GET /api/v1/jobs/{job_id}` — get aggregate transfer state.
- `GET /api/v1/jobs/{job_id}/events` — stage/progress/audit events.
- `POST /api/v1/jobs/{job_id}/pause` / `POST /api/v1/jobs/{job_id}/resume` — supported when the active Fetch adapter can pause.
- `POST /api/v1/jobs/{job_id}/cancel` — cancel active stage/provider work.
- `POST /api/v1/jobs/{job_id}/retry` — retry the failed stage in-place where durable stage data exists.
- `POST /api/v1/jobs/{job_id}/retry-with-strategy` — bounded resolver/engine retry strategy.
- `POST /api/v1/jobs/{job_id}/ack` — mark READY content RETRIEVED and clean Tokyo working data.
- `DELETE /api/v1/jobs/{job_id}/history` / `DELETE /api/v1/jobs/history` — clear terminal history only.

## Receiver

- `GET /api/v1/jobs/{job_id}/receiver-ticket` — READY QQ Flash ticket, schema v2. The ticket carries the stable `fileSetId` plus public share fallback metadata; it does not carry a long-lived CDN URL.
- `GET /api/v1/jobs/{job_id}/receiver-url?artifact=N` — authenticated resolver for a fresh, short-lived QQ CDN URL. On a runtime-capable node P20 prefers `fileSetId -> QQNT getFileList -> startFileTransferUrl`; the QQ Runtime may be woken briefly on demand. If that capability is unavailable or fails, P20 falls back to the public `qfile.qq.com` share layer. File bytes are never proxied through Hub and responses use `Cache-Control: no-store`.
- When the public share layer is still propagating, `receiver-url` returns HTTP 503 with `status=WAITING_FOR_QQ_SHARE`, `retryable=true`, `waited_seconds`, a staged `retry_after` value (1/15/30/60 seconds), runtime context, and safe alternatives. This is a waiting state rather than a failed transfer; callers must not recreate the job, redownload the source, or re-upload QQ. Browser clients stop automatic waiting after about 10 seconds, while `p20-receive` may follow `retry_after` for a longer bounded window.

## Resolution and diagnostics

- `POST /api/v1/resolve` — deterministic source analysis only; does not create a transfer.
- `GET /api/v1/ui-context` — safe/redacted job context for clients.
- `GET /api/v1/failure-fingerprints` and `GET /api/v1/failure-fingerprints/{fingerprint_id}` — deterministic failure signatures.
- Skill registry: `GET/POST /api/v1/skills`, `GET /api/v1/skills/{skill_id}`, `POST /api/v1/skills/{skill_id}/promote`, and `POST /api/v1/jobs/{job_id}/apply-skill`.
- `POST /api/v1/jobs/{job_id}/revoke-share` — explicit, confirmation-gated irreversible action where supported.

P20 0.5.x has no Sidechat, Supervisor, Codex or AI repair routes. Requests to those retired paths return 404.

## Watchers / scheduled tasks

P20 Watcher is a lightweight always-on control-plane component. It checks external release feeds while the heavy Transfer Hub and QQ Runtime remain asleep, and wakes the Hub only when a new artifact needs a normal P20 transfer.

- `GET /api/v1/watchers` — list configured watcher tasks, schedule state, latest observed item and linked P20 job state.
- `GET /api/v1/watcher-runs` — list recent watcher executions. Outcomes are `CREATED`, `SKIPPED`, `BOOTSTRAPPED`, `FAILED` or transient `RUNNING`.
- `POST /api/v1/watchers/test-notification` — authenticated WebUI test endpoint. It sends a synthetic `p20.watcher.test` event through the configured Hermes webhook without creating a transfer.
- `POST /api/v1/watchers/{watcher_id}/enqueue` — internal watchd-to-Hub handoff. It validates the active watcher/run and creates a normal transfer tagged with `scheduled=true`, `watcher_id`, `watcher_name`, `watch_run_id` and `watch_notify=true` when READY notification is enabled.

Watcher-created transfers still use the ordinary P20 Job/Receiver contract. A watcher never installs or executes downloaded software. The initial observation defaults to `BOOTSTRAPPED`, so enabling a watcher does not backfill historical releases. When a newly created watcher job reaches `READY`, `p20-watchd` emits one signed `p20.watcher.ready` webhook; notification failure does not mutate or retry the transfer itself.

## Runtime control

- `GET /api/v1/runtime/status`
- `POST /api/v1/runtime/start`
- `POST /api/v1/runtime/stop`

These endpoints are operational QQ controls. Normal UI access and Fetch do not require callers to start QQ; Deliver starts it automatically when needed.

## Local files and compatibility

- `GET /api/v1/local-files` and `POST /api/v1/local-files/jobs` create transfers from explicitly allowed VPS roots.
- `POST /api/v1/uploads` remains a legacy compatibility endpoint; the normal 0.5 Web UI does not upload browser-local files to create transfers.
- `GET /api/v1/events` provides the SSE event stream.

## CMCloud component

The CMCloud transit API remains separate from the P20 Job Queue:

- `/api/v1/cmcloud/status`
- `/api/v1/cmcloud/refresh`
- `/api/v1/cmcloud/list`
- `/api/v1/cmcloud/uploads`
- `/api/v1/cmcloud/uploads/{session_id}/parts`
- `/api/v1/cmcloud/uploads/{session_id}/complete`
- `/api/v1/cmcloud/downloads/{session_id}`
- `/api/v1/cmcloud/{session_id}/trash`

Its E2EE wire format is documented in `P20_CMCLOUD_COMPONENT.md` and is not the P20 Job Queue implementation.

## Contract checks

Release self-checks instantiate the FastAPI app and inspect `app.openapi()`/registered routes. `docs/API.md` is descriptive; the generated OpenAPI schema wins if prose and code ever disagree. Any deliberate route removal/addition must update this file and the MCP compatibility tests before release.
