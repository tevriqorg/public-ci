from pathlib import Path

from fastapi.testclient import TestClient

from transfer_hub.api import create_app
from transfer_hub.config import Settings
from transfer_hub.control import SkillRegistry, fingerprint
from transfer_hub.db import Store
from transfer_hub.domain import JobState
from transfer_hub.delivery import QQFlashDelivery
from transfer_hub.service import TransferService
from transfer_hub.resolver import ResolverError
import transfer_hub.service as service_module


def test_fingerprint_stable_and_secret_free(tmp_path: Path):
    job = {"source": "https://media.example/video?token=private", "source_type": "url", "engine": "aria2"}
    first = fingerprint(job, "direct", RuntimeError("Cookie: sid=secret"))
    second = fingerprint(job, "direct", RuntimeError("different raw secret"))
    assert first == second
    assert "private" not in str(first) and "secret" not in str(first)


def test_skill_candidate_explicit_promotion_and_degradation(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    registry = SkillRegistry(store)
    skill = registry.create({"name": "example", "rules": {"domain": "example.test"}, "config": {"resolver": "direct", "engine": "aria2", "auth_ref": "safe_ref"}})
    assert registry.matching("https://example.test/a", "url") is None
    job = store.create_job({"id": "job", "source_type": "url", "source": "https://example.test/a", "filename": "a", "title": "a", "engine": "auto", "options": {}, "secret": {}})
    registry.apply(skill["id"], job, explicit=True)
    assert store.get_job("job")["options"]["applied_skill_id"] == skill["id"]
    registry.promote(skill["id"], "validated")
    assert registry.matching("https://sub.example.test/a", "url")["id"] == skill["id"]
    registry.promote(skill["id"], "stable")
    assert store.get_skill(skill["id"])["state"] == "stable"
    for _ in range(3): registry.validate(skill["id"], False, "EngineError")
    assert store.get_skill(skill["id"])["state"] == "degraded"
    assert registry.matching("https://example.test/a", "url") is None


def _app(tmp_path: Path):
    token = tmp_path / "token"; token.write_text("test")
    settings = Settings(tmp_path, "127.0.0.1", 0, token, tmp_path / "secrets", "http://127.0.0.1:9", tmp_path / "none")
    return create_app(settings, start_worker=False)


def test_control_endpoints_redaction_and_ai_routes_removed(tmp_path: Path):
    app = _app(tmp_path)
    job = app.state.service.create_url_job({"source": "https://example.test/a?token=secret", "source_type": "url", "delivery_backend": "none"})
    app.state.store.update(job["id"], state="READY", result={"backend": "qq_flash", "items": [{"share_link": "https://share.invalid/x"}]})
    headers = {"Authorization": "Bearer test"}
    with TestClient(app) as client:
        created = client.post("/api/v1/skills", headers=headers, json={"name": "test", "rules": {"domain": "example.test"}, "config": {"auth_ref": "secret_ref"}})
        assert created.status_code == 201
        bad = client.post("/api/v1/skills", headers=headers, json={"name": "bad", "config": {"headers": {"Cookie": "secret"}}})
        assert bad.status_code == 400
        ctx = client.get(f"/api/v1/ui-context?job_id={job['id']}", headers=headers).json()
        assert "secret" not in ctx["source"]
        pending = client.post(f"/api/v1/jobs/{job['id']}/revoke-share", headers=headers, json={}).json()
        assert pending["status"] == "confirmation_required"
        assert client.post("/api/v1/sidechat", headers=headers, json={}).status_code == 404
        assert client.get("/api/v1/supervisor/messages", headers=headers).status_code == 404
        health = client.get("/health").json()
        assert "ai" not in health["capabilities"]

def test_resolve_endpoint_validates_url_and_does_not_start_job(tmp_path: Path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/v1/resolve", headers={"Authorization": "Bearer test"}, json={"source": "http://127.0.0.1/private"})
        assert response.status_code == 400
        assert app.state.store.list_jobs() == []


def test_transfer_failure_persists_safe_fingerprint_and_audit(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    service: TransferService = app.state.service
    job = service.create_url_job({"source": "https://example.test/a?token=never-store-this", "source_type": "url", "delivery_backend": "none"})
    def fail(*args, **kwargs): raise ResolverError("failed Cookie: secret-value")
    monkeypatch.setattr(service_module, "resolve_source", fail)
    service._execute(job)
    failed = app.state.store.get_job(job["id"])
    assert failed["state"] == JobState.FAILED
    assert failed["options"]["failure_fingerprint"]
    assert "never-store-this" not in str(app.state.store.list_fingerprints())
    assert any(event["type"] == "failure.fingerprinted" for event in app.state.store.recent_events(job["id"]))
    assert any(event["type"] == "fallback.result" for event in app.state.store.recent_events(job["id"]))



def test_download_403_uses_browser_fallback_headers(tmp_path: Path, monkeypatch):
    import json
    import urllib.request
    from transfer_hub.engines import EngineError
    from transfer_hub.resolver import ResolutionPlan, ResolvedItem

    service = _app(tmp_path).state.service
    job = service.create_url_job({
        "source": "https://example.com/video.m3u8",
        "source_type": "auto",
        "delivery_backend": "none",
        "pack": False,
        "encrypt": False,
    })
    monkeypatch.setenv("P20_BROWSER_AGENT_URL", "http://127.0.0.1:19991")

    class R:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n=-1):
            return json.dumps({
                "url": "https://example.com/video.m3u8",
                "engine": "nm3u8dl",
                "headers": {"Cookie": "cf_clearance=ok", "Referer": "https://site.example/watch/1"},
            }).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: R())
    monkeypatch.setattr(
        service_module,
        "resolve_source",
        lambda *args, **kwargs: ResolutionPlan(
            "manifest",
            [ResolvedItem("https://example.com/video.m3u8", "nm3u8dl", "video.m3u8", "hls", {})],
        ),
    )
    calls = []

    def fake_download(engine, source, out_dir, filename, headers, *args, **kwargs):
        calls.append((engine, dict(headers)))
        if len(calls) == 1:
            raise EngineError("HTTP 403 Forbidden Cloudflare challenge")
        out_dir.mkdir(parents=True, exist_ok=True)
        result = out_dir / "video.mp4"
        result.write_bytes(b"ok")
        return result

    monkeypatch.setattr("transfer_hub.fetch_adapters.download", fake_download)
    monkeypatch.setattr(service_module, "pack_zip_aes", lambda source, output, password, volume_size, cancel: ((output.write_bytes(b"archive"), [output])[1]))
    monkeypatch.setattr(service_module, "verify_7z_password", lambda *_args, **_kwargs: None)
    service._execute(job)

    done = service.store.get_job(job["id"])
    assert done["state"] == JobState.READY, done["error"]
    assert len(calls) == 2
    assert calls[1][0] == "nm3u8dl"
    assert calls[1][1]["Cookie"] == "cf_clearance=ok"
    assert calls[1][1]["Referer"] == "https://site.example/watch/1"
    types = [event["type"] for event in service.store.recent_events(job["id"], 100)]
    assert "download.protected_source" in types
    assert "fallback.result" in types


def test_retry_strategies_are_bounded(tmp_path: Path):
    service = _app(tmp_path).state.service
    job = service.create_url_job({"source": "https://example.test/a", "source_type": "url", "delivery_backend": "none"})
    service.store.update(job["id"], state="FAILED", error="failed")
    try:
        service.retry_with_strategy(job["id"], "--exec arbitrary")
    except ValueError:
        pass
    else:
        raise AssertionError("arbitrary strategy accepted")
    retried = service.retry_with_strategy(job["id"], "direct")
    assert retried["state"] == "QUEUED"
    assert retried["options"]["resolver_override"] == "direct"


def test_browser_revoke_requires_confirmation_and_verifies(tmp_path: Path, monkeypatch):
    import urllib.request, json
    service = _app(tmp_path).state.service
    job = service.create_url_job({"source": "https://example.test/a", "source_type": "url", "delivery_backend": "none"})
    service.store.update(job["id"], state="READY", result={"backend": "qq_flash", "items": [{"share_link": "https://share.invalid/x"}]})
    monkeypatch.setenv("P20_BROWSER_AGENT_URL", "http://127.0.0.1:19991")
    calls = []
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n=-1): return json.dumps({"status": "revoked", "verified_invalid": True}).encode()
    def fake(req, timeout=0): calls.append((req.full_url, timeout)); return R()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    pending = service.revoke_share(job["id"], False)
    assert pending["status"] == "confirmation_required" and calls == []
    done = service.revoke_share(job["id"], True)
    assert done["status"] == "completed"
    assert calls == [("http://127.0.0.1:19991/operations/revoke_qq_flash_share", 8)]


def test_command_engine_hint_does_not_force_manual_resolver(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    token = tmp_path / "token"; token.write_text("x")
    settings = Settings(tmp_path, "127.0.0.1", 0, token, tmp_path / "secrets", "http://127.0.0.1:9", token)
    service = TransferService(settings, store)
    job = service.create_url_job({
        "source": "https://example.test/video.m3u8",
        "source_type": "auto",
        "engine": "auto",
        "command_engine_hint": "nm3u8dl",
        "delivery_backend": "none",
    })
    assert job["options"]["engine_override"] == "auto"
    assert job["options"]["command_engine_hint"] == "nm3u8dl"
