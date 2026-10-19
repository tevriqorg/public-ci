from __future__ import annotations

from app.models import LocalAsset


_MP4_CONTAINERS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
_BROWSER_VIDEO_CODECS = {"h264", "avc1"}
_BROWSER_AUDIO_CODECS = {"", "aac", "mp3"}


def browser_can_play_directly(asset: LocalAsset) -> bool:
    """Return whether a verified asset can be handed to a browser unchanged."""
    containers = set((asset.container or "").lower().split(","))
    return (
        bool(containers & _MP4_CONTAINERS)
        and (asset.video_codec or "").lower() in _BROWSER_VIDEO_CODECS
        and (asset.audio_codec or "").lower() in _BROWSER_AUDIO_CODECS
    )
