from pathlib import Path

from transfer_hub.db import Store
from transfer_hub.watchers import parse_github_release, parse_sparkle_appcast


def test_watch_store_records_tasks_and_runs(tmp_path: Path):
    store = Store(tmp_path / "watch.db")
    task = store.seed_watch_task({
        "id": "demo", "name": "Demo", "kind": "github_release",
        "interval_seconds": 300, "config": {"url": "https://example.com/releases"},
    })
    assert task["enabled"] is True
    run = store.start_watch_run("demo")
    finished = store.finish_watch_run(run["id"], outcome="SKIPPED", item_key="v1", item_title="Version 1")
    assert finished["outcome"] == "SKIPPED"
    assert store.list_watch_runs("demo", 5)[0]["item_key"] == "v1"


def test_github_release_parser_selects_latest_matching_asset():
    payload = [
        {"id": 2, "tag_name": "v2", "published_at": "2026-10-12T01:00:00Z", "draft": False, "prerelease": True,
         "assets": [{"id": 22, "name": "Signal-v2.apk", "browser_download_url": "https://example.com/v2.apk", "size": 20}]},
        {"id": 1, "tag_name": "v1", "published_at": "2026-10-11T01:00:00Z", "draft": False, "prerelease": False,
         "assets": [{"id": 11, "name": "Signal-v1.apk", "browser_download_url": "https://example.com/v1.apk", "size": 10}]},
    ]
    result = parse_github_release(payload, {"asset_suffix": ".apk", "include_prerelease": True,
                                            "title_template": "Signal {version} · Android APK"})
    assert result.key == "github-asset:22"
    assert result.filename == "Signal-v2.apk"
    assert result.version == "v2"


def test_sparkle_parser_selects_arm64_enclosure():
    xml = b'''<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>
      <item><sparkle:shortVersionString>26.1007.30912</sparkle:shortVersionString><sparkle:version>30912</sparkle:version>
      <sparkle:hardwareRequirements>arm64</sparkle:hardwareRequirements>
      <sparkle:enclosure url="https://persistent.oaistatic.com/ChatGPT-darwin-arm64-30912.zip" length="123" /></item>
    </channel></rss>'''
    result = parse_sparkle_appcast(xml, {"url_contains": "ChatGPT-darwin-arm64", "arch": "arm64",
                                         "title_template": "ChatGPT Desktop {version} · macOS arm64",
                                         "filename_template": "ChatGPT-{version}-arm64.zip"})
    assert result.version == "26.1007.30912"
    assert result.filename == "ChatGPT-26.1007.30912-arm64.zip"
    assert result.key.startswith("sparkle:30912:")


def test_watcher_enqueue_marks_job_as_scheduled(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient
    import transfer_hub.api as api_module
    from transfer_hub.api import create_app
    from transfer_hub.config import Settings

    token_file = tmp_path / "api.token"
    token_file.write_text("test-token")
    settings = Settings(
        data_dir=tmp_path, bind="127.0.0.1", port=0, api_token_file=token_file,
        secrets_dir=tmp_path / "secrets", qqxfer_url="http://127.0.0.1:9",
        qqxfer_token_file=tmp_path / "missing-qqxfer-token",
    )
    app = create_app(settings=settings, start_worker=False)
    task = app.state.store.seed_watch_task({
        "id": "demo", "name": "Demo Watch", "kind": "github_release", "interval_seconds": 300,
        "config": {"url": "https://example.com/releases", "delivery_profile": "android", "delivery_backend": "none"},
    })
    run = app.state.store.start_watch_run(task["id"])
    monkeypatch.setattr(api_module, "validate_public_http_url", lambda _: None)
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/watchers/demo/enqueue", headers=headers,
            json={
                "watch_run_id": run["id"], "source": "https://example.com/demo.apk",
                "filename": "demo.apk", "title": "Demo v1", "version": "v1", "details": {},
            },
        )
        assert response.status_code == 202
        job = response.json()
        assert job["options"]["scheduled"] is True
        assert job["options"]["watcher_id"] == "demo"
        assert job["options"]["watch_run_id"] == run["id"]
        assert job["options"]["watch_notify"] is True
        listing = client.get("/api/v1/watchers", headers=headers).json()
        assert listing[0]["id"] == "demo"


def test_host_gh_release_inspection_uses_gh_runner(monkeypatch):
    import json
    import transfer_hub.watchers as watcher_module

    payload = [{
        "id": 7, "tag_name": "private-v1", "published_at": "2026-10-15T00:00:00Z",
        "draft": False, "prerelease": True,
        "assets": [{
            "id": 77, "name": "private.apk",
            "browser_download_url": "https://github.com/example/private/releases/download/v1/private.apk",
            "size": 123,
        }],
    }]
    seen = {}

    def fake_gh(url, *, accept="*/*", timeout=30):
        seen.update(url=url, accept=accept, timeout=timeout)
        return json.dumps(payload).encode()

    monkeypatch.setattr(watcher_module, "_request_bytes_via_gh", fake_gh)
    result = watcher_module.inspect_task({
        "kind": "github_release",
        "config": {
            "url": "https://api.github.com/repos/example/private/releases?per_page=10",
            "runner": "host_gh", "asset_suffix": ".apk", "include_prerelease": True,
        },
    })
    assert result.key == "github-asset:77"
    assert seen["accept"] == "application/vnd.github+json"


def test_host_gh_enqueue_downloads_verifies_and_marks_scheduled(tmp_path: Path, monkeypatch):
    import hashlib
    import json
    import subprocess
    import transfer_hub.watchd as watchd_module
    from transfer_hub.watchd import HubClient
    from transfer_hub.watchers import WatchCandidate

    payload = b"private-apk-bytes"

    class FakeStore:
        def __init__(self):
            self.updated = None
            self.events = []

        def update(self, job_id, **fields):
            self.updated = (job_id, fields)
            return {"id": job_id, "job_number": 42, "options": fields["options"], "title": fields["title"]}

        def event(self, job_id, event_type, data):
            self.events.append((job_id, event_type, data))

    store = FakeStore()

    def fake_run(args, **kwargs):
        if args[0] == "gh":
            kwargs["stdout"].write(payload)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        assert args[0] == "curl"
        upload_arg = next(arg for arg in args if arg.startswith("@"))
        assert Path(upload_arg[1:]).read_bytes() == payload
        body = json.dumps({"id": "job-private", "job_number": 42, "options": {"delivery_profile": "unified"}}).encode()
        return subprocess.CompletedProcess(args, 0, body, b"")

    monkeypatch.setattr(watchd_module.subprocess, "run", fake_run)
    candidate = WatchCandidate(
        key="github-asset:77", title="Private v1", version="v1",
        source="https://github.com/example/private/releases/download/v1/private.apk",
        filename="private.apk",
        details={"asset_id": 77, "size": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest()},
    )
    task = {
        "id": "private", "name": "Private Watch", "config": {
            "url": "https://api.github.com/repos/example/private/releases?per_page=10",
            "runner": "host_gh", "delivery_backend": "qq_flash", "notify_ready": True,
        },
    }
    client = HubClient("http://127.0.0.1:17610", store, tmp_path)
    job = client.enqueue(task, candidate, 9)
    assert job["job_number_label"] == "P20-000042"
    assert store.updated[1]["options"]["scheduled"] is True
    assert store.updated[1]["options"]["watcher_id"] == "private"
    assert store.updated[1]["options"]["watch_run_id"] == 9
    assert store.events[0][1] == "job.watcher"
    assert not list((tmp_path / "watcher-private").glob("*.asset"))
