from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request

from .domain import filename_from_url, safe_filename
from .security import open_public_http, validate_public_http_url

STREAM_SUFFIXES = (".m3u8", ".mpd", ".ism/manifest")
TWITTER_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
VIDEO_HOST_MARKERS = (
    "youtube.com/", "youtu.be/", "bilibili.com/", "b23.tv/", "vimeo.com/",
    "tiktok.com/", "douyin.com/", "instagram.com/",
)


class ResolverError(RuntimeError):
    pass


@dataclass
class ResolvedItem:
    source: str
    engine: str
    filename: str
    kind: str = "file"
    headers: dict[str, str] = field(default_factory=dict)
    engine_options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolutionPlan:
    resolver: str
    items: list[ResolvedItem]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def engine_label(self) -> str:
        engines = {item.engine for item in self.items}
        return next(iter(engines)) if len(engines) == 1 else "multi"


def _expires_info(url: str) -> dict[str, str]:
    query = parse_qs(urlparse(url).query)
    for key in ("expires", "expire", "exp", "e"):
        values = query.get(key)
        if values:
            return {"key": key, "value": values[0]}
    return {}


def _stream_kind(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if ".m3u8" in path:
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    if ".ism/manifest" in path:
        return "ism"
    return None


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _gallery_json(source: str, headers: dict[str, str]) -> list[Any]:
    exe = shutil.which("gallery-dl")
    if not exe:
        raise ResolverError("gallery-dl is not installed")
    argv = [exe, "-J", "--no-download"]
    for key, value in headers.items():
        argv += ["--header", f"{key}: {value}"]
    argv.append(source)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["unknown error"]
        raise ResolverError(f"gallery-dl resolve failed: {detail[0][:400]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ResolverError("gallery-dl returned invalid JSON") from exc
    return data if isinstance(data, list) else []


def _resolve_twitter(source: str, headers: dict[str, str], filename: str) -> ResolutionPlan:
    messages = _gallery_json(source, headers)
    media: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, list) or len(msg) < 3 or msg[0] != 3:
            continue
        meta = msg[2] if isinstance(msg[2], dict) else {}
        media.append({
            "type": str(meta.get("type") or "unknown"),
            "num": int(meta.get("num") or len(media) + 1),
            "extension": str(meta.get("extension") or ""),
            "url": str(msg[1]),
        })
    if not media:
        raise ResolverError("tweet contains no downloadable media")
    has_images = any(m["type"] == "photo" for m in media)
    has_video = any(m["type"] in {"video", "animated_gif", "gif"} for m in media)
    items: list[ResolvedItem] = []
    stem = safe_filename(Path(filename).stem or "tweet")
    if has_images:
        items.append(ResolvedItem(
            source=source,
            engine="gallery-dl",
            filename=f"{stem}-images",
            kind="images",
            headers=headers,
            engine_options={
                "twitter_images_only": True,
                "filename_format": "{author[name]}-{tweet_id}-{num}.{extension}",
            },
        ))
    if has_video:
        items.append(ResolvedItem(
            source=source,
            engine="yt-dlp",
            filename=f"{stem}-video",
            kind="video",
            headers=headers,
            engine_options={"allow_playlist": True},
        ))
    return ResolutionPlan(
        resolver="twitter",
        items=items,
        metadata={
            "media_count": len(media),
            "media": media,
            "has_images": has_images,
            "has_video": has_video,
        },
    )


def _extract_manifest_from_page(source: str, headers: dict[str, str]) -> tuple[str, dict[str, str]] | None:
    request_headers = {"User-Agent": "P20-Transfer-Hub/2"}
    request_headers.update(headers)
    req = Request(source, headers=request_headers)
    with open_public_http(req, timeout=20) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return None
        raw = resp.read(2 * 1024 * 1024)
        text = unescape(raw.decode(resp.headers.get_content_charset() or "utf-8", errors="replace"))
    candidates = re.findall(r'''(?P<url>(?:https?:)?//[^\s"'<>]+?(?:\.m3u8|\.mpd|\.ism/manifest)(?:\?[^\s"'<>]*)?|[^\s"'<>]+?(?:\.m3u8|\.mpd|\.ism/manifest)(?:\?[^\s"'<>]*)?)''', text, flags=re.I)
    if not candidates:
        return None
    manifest = candidates[0].replace("\\/", "/")
    manifest = urljoin(source, manifest)
    try:
        validate_public_http_url(manifest)
    except ValueError as exc:
        raise ResolverError(f"resolved manifest is not a public URL: {exc}") from exc
    derived = dict(headers)
    derived.setdefault("Referer", source)
    derived.setdefault("Origin", _origin(source))
    derived.setdefault("User-Agent", request_headers["User-Agent"])
    return manifest, derived


def _single_item(source: str, engine: str, filename: str, headers: dict[str, str], kind: str = "file") -> ResolutionPlan:
    return ResolutionPlan("direct", [ResolvedItem(source, engine, filename, kind, headers)])


def _force_engine(plan: ResolutionPlan, engine: str) -> ResolutionPlan:
    if engine == "auto":
        return plan
    for item in plan.items:
        item.engine = engine
        item.engine_options = {}
    plan.metadata["manual_engine"] = engine
    return plan


def resolve_source(
    source_type: str,
    source: str,
    filename: str | None,
    headers: dict[str, str] | None = None,
    engine_override: str = "auto",
    resolver_override: str = "auto",
) -> ResolutionPlan:
    headers = dict(headers or {})
    filename = safe_filename(filename or filename_from_url(source))
    parsed = urlparse(source)
    host = parsed.netloc.lower()

    from .uav import recognizes
    if engine_override in {"auto", "uav"} and resolver_override == "auto" and recognizes(source):
        return ResolutionPlan("uav", [ResolvedItem(source, "uav", filename, "video", headers)])
    if engine_override == "uav":
        raise ResolverError("UAV 需要 Jable、MissAV、SupJav 详情页或番号")

    if resolver_override == "twitter":
        return _force_engine(_resolve_twitter(source, headers, filename), engine_override)
    if resolver_override == "manifest":
        kind = _stream_kind(source) or "stream"
        return _force_engine(ResolutionPlan("manifest", [ResolvedItem(source, "nm3u8dl", filename, kind, headers)], {"manifest_url": source, "expires": _expires_info(source)}), engine_override)
    if resolver_override == "webpage-manifest":
        found = _extract_manifest_from_page(source, headers)
        if not found:
            raise ResolverError("no stream manifest found in source page")
        manifest, resolved_headers = found
        plan = ResolutionPlan("webpage-manifest", [ResolvedItem(manifest, "nm3u8dl", filename, _stream_kind(manifest) or "stream", resolved_headers)], {"manifest_url": manifest, "source_page": source, "expires": _expires_info(manifest), "header_names": sorted(resolved_headers)})
        return _force_engine(plan, engine_override)
    if resolver_override == "direct":
        return _single_item(source, engine_override if engine_override != "auto" else "aria2", filename, headers)

    if engine_override != "auto":
        return ResolutionPlan(
            "manual",
            [ResolvedItem(source, engine_override, filename, "manual", headers)],
            {"manual_engine": engine_override},
        )

    if host in TWITTER_HOSTS and "/status/" in parsed.path:
        return _resolve_twitter(source, headers, filename)

    kind = _stream_kind(source)
    if kind:
        return ResolutionPlan(
            "manifest",
            [ResolvedItem(source, "nm3u8dl", filename, kind, headers)],
            {"manifest_url": source, "source_page": None, "expires": _expires_info(source)},
        )
    t = source_type.lower()
    if t in {"stream", "m3u8", "hls", "dash", "ism"}:
        found = _extract_manifest_from_page(source, headers)
        if found:
            manifest, resolved_headers = found
            stream_kind = _stream_kind(manifest) or "stream"
            return ResolutionPlan(
                "webpage-manifest",
                [ResolvedItem(manifest, "nm3u8dl", filename, stream_kind, resolved_headers)],
                {
                    "manifest_url": manifest,
                    "source_page": source,
                    "expires": _expires_info(manifest),
                    "header_names": sorted(resolved_headers),
                },
            )
        return _single_item(source, "nm3u8dl", filename, headers, "stream")

    lower = source.lower()
    if t in {"video", "web_video", "playlist"} or any(marker in lower for marker in VIDEO_HOST_MARKERS):
        return _single_item(source, "yt-dlp", filename, headers, "video")

    if t in {"auto", "url"}:
        try:
            found = _extract_manifest_from_page(source, headers)
        except Exception:
            found = None
        if found:
            manifest, resolved_headers = found
            stream_kind = _stream_kind(manifest) or "stream"
            return ResolutionPlan(
                "webpage-manifest",
                [ResolvedItem(manifest, "nm3u8dl", filename, stream_kind, resolved_headers)],
                {
                    "manifest_url": manifest,
                    "source_page": source,
                    "expires": _expires_info(manifest),
                    "header_names": sorted(resolved_headers),
                },
            )

    return _single_item(source, "aria2", filename, headers)
