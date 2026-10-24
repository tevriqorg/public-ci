# P20 MCP Contract 0.5.5

MCP is a thin deterministic adapter over P20 REST. It does not host a model or AI supervisor. The default transport remains stdio; network exposure is not required for the Tokyo service.

## Transfer tools

- `create_transfer`
- `get_transfer`
- `list_transfers`
- `clear_transfer`
- `clear_transfer_history`
- `pause_transfer`
- `resume_transfer`
- `cancel_transfer`
- `retry_transfer`
- `get_receiver_ticket`
- `get_receiver_url`
- `acknowledge_transfer`

`create_transfer` accepts source, source type, optional engine/resolver hints and delivery profile. Desktop/mobile policy is server-owned; MCP no longer pretends `pack` or `encrypt` can override the selected profile.

`retry_transfer` retries the failed durable stage in place where possible, preserving completed upstream artifacts.

## Standard consumer workflow

For another project or Agent, MCP is only the control side of the P20 channel. The normal automated flow is:

```text
create_transfer -> get_transfer until READY -> get_receiver_ticket -> run p20-receive on destination -> ACK -> DONE
```

`READY` is not final completion for the consuming project. `get_receiver_ticket` also does not complete retrieval by itself. The destination must run `p20-receive`, which verifies size/SHA256, performs required decrypt/extract processing, and normally sends ACK only after that succeeds.

`get_receiver_url(job_id, artifact)` exposes the resolver state for agents that need it directly. P20 first asks a usable local QQ Runtime to generate a fresh temporary URL from the stable `fileSetId`. If that path is unavailable, it falls back to the public QQ share layer. A retryable result with `status=WAITING_FOR_QQ_SHARE` explains why the caller is waiting and includes `waited_seconds`, `retry_after`, runtime context, and alternatives. It is not a transfer failure and does not require recreating the job or re-uploading the file.

Browser Receiver is a human WebUI path. Project automation should follow `docs/P20_CHANNEL_USAGE.md` and use Receiver Ticket + CLI.

## Deterministic inspection and control

- `analyze_transfer`
- `resolve_source`
- `get_transfer_logs`
- `get_ui_context`
- `diagnose_failure` — returns persisted fingerprint/error/events only; no repair model is invoked.
- `retry_with_strategy`
- `revoke_share`

## Skill registry

- `list_skills`
- `get_skill`
- `apply_skill`
- `promote_skill`

## Runtime operations

- `check_runtime`
- `start_runtime`
- `stop_runtime`

QQ runtime tools are explicit operational controls. Ordinary P20 transfers do not require an MCP caller to start QQ; the Deliver stage starts it automatically for QQ Flash.

The curated surface intentionally excludes internal stage claims, artifact paths, raw secrets, arbitrary shell/downloader arguments and retired Sidechat/Supervisor/Codex operations.
