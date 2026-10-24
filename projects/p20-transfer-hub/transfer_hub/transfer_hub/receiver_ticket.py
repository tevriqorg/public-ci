from __future__ import annotations

import re
from urllib.parse import urljoin

from .domain import JobState, safe_filename

_TICKET_SCHEMA = "p20.receiver-ticket/v2"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def build_receiver_ticket(job: dict, base_url: str) -> dict:
    if job.get("state") != JobState.READY:
        raise ValueError("receiver ticket is available only while job is READY")
    result = job.get("result") or {}
    if result.get("backend") != "qq_flash":
        raise ValueError("receiver ticket requires qq_flash delivery")
    artifacts = []
    for index, item in enumerate(result.get("items") or []):
        if not item.get("ok"):
            continue
        share_link = str(item.get("share_link") or "").strip()
        digest = str(item.get("sha256") or "").lower()
        if not share_link.startswith("https://qfile.qq.com/q/"):
            raise ValueError("qq_flash item is missing an official share link")
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("qq_flash item is missing sha256")
        resolve_path = f"/api/v1/jobs/{job['id']}/receiver-url?artifact={index}"
        artifacts.append({
            "name": safe_filename(str(item.get("filename") or "download.bin")),
            "size": int(item.get("size") or 0),
            "sha256": digest,
            "source": {
                "kind": "qq_flash_share",
                "share_link": share_link,
                "file_set_id": str(item.get("file_set_id") or (item.get("flash_status") or {}).get("file_set_id") or ""),
            },
            "browser_resolve_path": resolve_path,
            "browser_resolve_url": urljoin(base_url.rstrip("/") + "/", resolve_path.lstrip("/")),
        })
    if not artifacts:
        raise ValueError("qq_flash delivery has no receiver artifacts")
    options = job.get("options") or {}
    password = (job.get("secret") or {}).get("archive_password")
    postprocess = {"kind": "none"}
    if options.get("pack"):
        postprocess = {
            "kind": "archive",
            "format": options.get("pack_format") or "zip-aes256",
            "encrypted": bool(options.get("encrypt")),
            "password": password if options.get("encrypt") else None,
            "primary": artifacts[0]["name"],
            "remove_archive_after_success": True,
        }
    ack_url = urljoin(base_url.rstrip("/") + "/", f"api/v1/jobs/{job['id']}/ack")
    return {
        "schema": _TICKET_SCHEMA,
        "transfer_id": job["id"],
        "artifacts": artifacts,
        "postprocess": postprocess,
        "ack": {"required": True, "method": "POST", "url": ack_url},
    }
