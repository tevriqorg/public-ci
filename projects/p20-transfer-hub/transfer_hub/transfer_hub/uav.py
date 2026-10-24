"""Strict routing for the pinned UAV download engine."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def media_code(value: str) -> str | None:
    value = value.strip().upper()
    match = re.fullmatch(r"FC2[\s-]*PPV[\s-]*(\d{5,9})", value)
    if match:
        return "FC2-PPV-" + match[1]
    match = re.fullmatch(r"([0-9]{0,3}[A-Z]{2,12})[\s-]*(\d{2,8})", value)
    return f"{match[1]}-{match[2]}" if match else None


def recognizes(value: str) -> bool:
    if media_code(value):
        return True
    try:
        p = urlsplit(value)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return False
        host = (p.hostname or "").lower()
        if host in {"jable.tv", "fs1.app"}:
            return bool(re.fullmatch(r"/videos/[A-Za-z0-9_-]+/", p.path))
        if host in {"missav.ai", "missav.ws", "missav.live", "missav123.com", "www.missav.ai", "www.missav.ws", "www.missav.live", "www.missav123.com"}:
            return bool(re.fullmatch(r"/(?:dm\d+/)?(?:(?:cn|en|ja|ko|ms|th)/)?[A-Za-z0-9][A-Za-z0-9_-]*[-_]\d[A-Za-z0-9_-]*", p.path))
        if host == "supjav.com":
            return bool(re.fullmatch(r"/(?:(?:zh|ja)/)?\d+\.html", p.path))
    except ValueError:
        pass
    return False


def candidates(value: str) -> list[str]:
    code = media_code(value)
    if code:
        slug = code.lower()
        return [f"https://missav.ai/en/{slug}", f"https://jable.tv/videos/{slug}/"]
    if not recognizes(value):
        raise ValueError("UAV 仅支持 Jable、MissAV、SupJav 详情页或番号")
    return [value]
