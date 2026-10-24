from __future__ import annotations

from transfer_hub import resolver


def test_twitter_photo_uses_gallery_dl(monkeypatch):
    monkeypatch.setattr(resolver, "_gallery_json", lambda *_: [
        [3, "https://pbs.twimg.com/media/a?format=jpg&name=orig", {"type": "photo", "num": 1, "extension": "jpg"}],
    ])
    plan = resolver.resolve_source("auto", "https://x.com/u/status/123", "tweet.bin")
    assert plan.resolver == "twitter"
    assert plan.engine_label == "gallery-dl"
    assert plan.items[0].engine_options["twitter_images_only"] is True
    assert plan.metadata["media_count"] == 1


def test_twitter_mixed_builds_multi_engine_plan(monkeypatch):
    monkeypatch.setattr(resolver, "_gallery_json", lambda *_: [
        [3, "https://pbs.twimg.com/media/a?format=jpg&name=orig", {"type": "photo", "num": 1, "extension": "jpg"}],
        [3, "https://video.twimg.com/a.mp4", {"type": "video", "num": 2, "extension": "mp4"}],
    ])
    plan = resolver.resolve_source("auto", "https://twitter.com/u/status/456", "tweet.bin")
    assert plan.engine_label == "multi"
    assert [(x.kind, x.engine) for x in plan.items] == [("images", "gallery-dl"), ("video", "yt-dlp")]


def test_twitter_gif_uses_ytdlp(monkeypatch):
    monkeypatch.setattr(resolver, "_gallery_json", lambda *_: [
        [3, "https://video.twimg.com/gif.mp4", {"type": "animated_gif", "num": 1, "extension": "mp4"}],
    ])
    plan = resolver.resolve_source("auto", "https://x.com/u/status/789", "tweet.bin")
    assert plan.engine_label == "yt-dlp"
    assert plan.items[0].kind == "video"


def test_bare_manifest_preserves_query_and_expiry():
    url = "https://cdn.example.com/live/master.m3u8?token=secret&expires=1893456000"
    plan = resolver.resolve_source("auto", url, "live.mp4", {"Cookie": "sid=x", "User-Agent": "UA"})
    item = plan.items[0]
    assert plan.resolver == "manifest"
    assert item.engine == "nm3u8dl"
    assert item.source == url
    assert item.headers["Cookie"] == "sid=x"
    assert plan.metadata["expires"] == {"key": "expires", "value": "1893456000"}


def test_webpage_manifest_preserves_source_context(monkeypatch):
    manifest = "https://cdn.example.com/live/master.m3u8?token=abc&exp=123"
    resolved_headers = {
        "Cookie": "sid=x", "Referer": "https://site.example/watch/1",
        "Origin": "https://site.example", "User-Agent": "UA",
    }
    monkeypatch.setattr(resolver, "_extract_manifest_from_page", lambda *_: (manifest, resolved_headers))
    plan = resolver.resolve_source("url", "https://site.example/watch/1", "watch.mp4", {"Cookie": "sid=x"})
    assert plan.resolver == "webpage-manifest"
    assert plan.items[0].source == manifest
    assert plan.items[0].headers == resolved_headers
    assert plan.metadata["source_page"] == "https://site.example/watch/1"
    assert plan.metadata["expires"] == {"key": "exp", "value": "123"}


def test_manual_resolver_and_engine_override(monkeypatch):
    monkeypatch.setattr(resolver, "_gallery_json", lambda *_: [
        [3, "https://pbs.twimg.com/media/a?format=jpg&name=orig", {"type": "photo", "num": 1, "extension": "jpg"}],
    ])
    plan = resolver.resolve_source("url", "https://x.com/u/status/123", "tweet.bin", engine_override="aria2", resolver_override="twitter")
    assert plan.resolver == "twitter"
    assert plan.engine_label == "aria2"
    assert plan.metadata["manual_engine"] == "aria2"
