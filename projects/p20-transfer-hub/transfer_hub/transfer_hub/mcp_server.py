from __future__ import annotations

import os
from typing import Literal

from mcp.server import MCPServer

from .rest_client import TransferAPIClient

mcp = MCPServer(
    "P20 Tokyo Transfer Hub",
    instructions=(
        "Use these tools to create and manage P20 transfer jobs. "
        "This server is a thin adapter over the P20 REST API and does not expose shell commands "
        "or raw downloader parameters."
    ),
)


def client() -> TransferAPIClient:
    return TransferAPIClient()


@mcp.tool()
def check_runtime() -> dict:
    """Report whether the QQ runtime is running and logged in.

    The QQ runtime is stopped by default to save host memory. Call this before
    a QQ Flash delivery, or use start_runtime to bring it up explicitly. A
    create_transfer with delivery_backend="qq_flash" starts it automatically.
    """
    return client().request("GET", "/api/v1/runtime/status")


@mcp.tool()
def start_runtime() -> dict:
    """Start the QQ runtime on demand and wait until it is logged in."""
    return client().request("POST", "/api/v1/runtime/start")


@mcp.tool()
def stop_runtime() -> dict:
    """Stop the QQ runtime to release host memory. No-op if already stopped."""
    return client().request("POST", "/api/v1/runtime/stop")


@mcp.tool()
def create_transfer(
    source: str,
    source_type: Literal["url", "github", "stream", "video"] = "url",
    filename: str | None = None,
    title: str | None = None,
    volume_size: str | None = None,
    delivery_backend: Literal["auto", "qq_flash", "none"] = "auto",
    ttl_seconds: int = 86400,
    auth_ref: str | None = None,
) -> dict:
    """Create an asynchronous transfer job from a public HTTP/HTTPS source.

    The default delivery_backend="auto" uses QQ Flash and starts the QQ
    runtime on demand if it is stopped. Use check_runtime first if you want
    to inspect or pre-warm it.
    """
    return client().request("POST", "/api/v1/jobs", {
        "source": source,
        "source_type": source_type,
        "filename": filename,
        "title": title,
        "engine": "auto",
        "delivery_profile": "unified",
        "volume_size": volume_size,
        "delivery_backend": delivery_backend,
        "ttl_seconds": ttl_seconds,
        "ack_policy": "required",
        "auth_ref": auth_ref,
    })


@mcp.tool()
def get_transfer(job_id: str, reveal_archive_password: bool = False) -> dict:
    """Get one transfer job and its current stage, progress, hashes, and delivery result."""
    suffix = "?reveal_secret=true" if reveal_archive_password else ""
    return client().request("GET", f"/api/v1/jobs/{job_id}{suffix}")


@mcp.tool()
def list_transfers(limit: int = 50) -> list[dict]:
    """List recent transfer jobs."""
    limit = max(1, min(limit, 200))
    return client().request("GET", f"/api/v1/jobs?limit={limit}")


@mcp.tool()
def clear_transfer(job_id: str) -> dict:
    """Clear one DONE, FAILED, or CANCELLED transfer and its local artifacts."""
    return client().request("DELETE", f"/api/v1/jobs/{job_id}/history")


@mcp.tool()
def clear_transfer_history() -> dict:
    """Clear all DONE, FAILED, or CANCELLED transfers. Active and READY jobs are protected."""
    return client().request("DELETE", "/api/v1/jobs/history")


@mcp.tool()
def pause_transfer(job_id: str) -> dict:
    """Pause an active aria2 download when that job supports pausing."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/pause", {})


@mcp.tool()
def resume_transfer(job_id: str) -> dict:
    """Resume a paused aria2 download."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/resume", {})


@mcp.tool()
def cancel_transfer(job_id: str) -> dict:
    """Cancel a queued or running transfer job."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/cancel", {})


@mcp.tool()
def retry_transfer(job_id: str) -> dict:
    """Retry a failed transfer job."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/retry", {})


@mcp.tool()
def get_receiver_ticket(job_id: str) -> dict:
    """Return a credential-free P20 receiver ticket for a READY QQ Flash job.

    The ticket contains stable QQ Flash identity, official share fallback,
    expected size/SHA256, and the P20 ACK endpoint. Pass the JSON to
    `p20-receive --ticket -` on any trusted tailnet host. The destination never
    needs QQ credentials; the Hub may briefly wake its own runtime to mint a URL.
    """
    return client().request("GET", f"/api/v1/jobs/{job_id}/receiver-ticket")


@mcp.tool()
def get_receiver_url(job_id: str, artifact: int = 0) -> dict:
    """Resolve a READY transfer artifact to a short-lived QQ CDN URL.

    P20 first uses a local QQ Runtime when this node has one. If the node has
    no usable QQ Runtime, the result may be WAITING_FOR_QQ_SHARE with a clear
    reason, retry_after interval, waited_seconds, and safe alternatives. A
    waiting result is retryable and does not mean the transfer or upload failed.
    """
    artifact = max(0, min(int(artifact), 99))
    return client().request_status("GET", f"/api/v1/jobs/{job_id}/receiver-url?artifact={artifact}")


@mcp.tool()
def acknowledge_transfer(job_id: str) -> dict:
    """Acknowledge successful retrieval so P20 can clean its working copy."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/ack", {})


@mcp.tool()
def analyze_transfer(job_id: str) -> dict:
    """Return deterministic job diagnosis context and a safe failure fingerprint."""
    context = client().request("GET", f"/api/v1/ui-context?job_id={job_id}")
    return {"context": context, "diagnosis": context.get("error") or f"Job is {context['state']}"}


@mcp.tool()
def resolve_source(source: str, source_type: Literal["auto", "url", "github", "stream", "video"] = "auto") -> dict:
    """Analyze a public URL resolution plan without creating a download job."""
    return client().request("POST", "/api/v1/resolve", {"source": source, "source_type": source_type})


@mcp.tool()
def get_transfer_logs(job_id: str, limit: int = 30) -> list[dict]:
    """Read recent events for one transfer job."""
    return client().request("GET", f"/api/v1/jobs/{job_id}/events?limit={max(1, min(limit, 200))}")


@mcp.tool()
def get_ui_context(job_id: str | None = None) -> dict:
    """Get redacted global dashboard or job context for an assistant."""
    suffix = "?job_id=" + job_id if job_id else ""
    return client().request("GET", "/api/v1/ui-context" + suffix)


@mcp.tool()
def list_skills(state: Literal["candidate", "validated", "stable", "degraded"] | None = None) -> list[dict]:
    """List safe resolver Skills and lifecycle state."""
    return client().request("GET", "/api/v1/skills" + ("?state=" + state if state else ""))


@mcp.tool()
def get_skill(skill_id: str) -> dict:
    """Get one Skill record."""
    return client().request("GET", f"/api/v1/skills/{skill_id}")


@mcp.tool()
def apply_skill(job_id: str, skill_id: str) -> dict:
    """Explicitly apply a Skill to a job; candidates are only usable this way."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/apply-skill", {"skill_id": skill_id})


@mcp.tool()
def promote_skill(skill_id: str, state: Literal["validated", "stable"] = "validated") -> dict:
    """Promote a candidate to validated, or a validated Skill to stable."""
    return client().request("POST", f"/api/v1/skills/{skill_id}/promote", {"state": state})


@mcp.tool()
def retry_with_strategy(job_id: str, strategy: Literal["default", "direct", "video", "manifest"] = "default") -> dict:
    """Retry a failed job using one bounded named strategy."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/retry-with-strategy", {"strategy": strategy})


@mcp.tool()
def diagnose_failure(job_id: str) -> dict:
    """Return deterministic failure context and recent events; P20 does not run an AI repair loop."""
    result = client().request("GET", f"/api/v1/ui-context?job_id={job_id}")
    return {"fingerprint": result.get("failure_fingerprint"), "error": result.get("error"),
            "state": result.get("state"), "events": result.get("recent_events", [])}


@mcp.tool()
def revoke_share(job_id: str, confirm_irreversible: bool = False) -> dict:
    """Request QQ Flash revocation; requires explicit confirmation and reports adapter capability."""
    return client().request("POST", f"/api/v1/jobs/{job_id}/revoke-share", {"confirm_irreversible": confirm_irreversible})


def main() -> None:
    transport = os.getenv("P20_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
        return
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=os.getenv("P20_MCP_BIND", "127.0.0.1"),
            port=int(os.getenv("P20_MCP_PORT", "17612")),
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
        )
        return
    raise SystemExit("P20_MCP_TRANSPORT must be stdio or streamable-http")


if __name__ == "__main__":
    main()
