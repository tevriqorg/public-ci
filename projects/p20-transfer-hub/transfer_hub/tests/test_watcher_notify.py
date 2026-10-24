from pathlib import Path
import hashlib
import hmac
import json
import urllib.request

import pytest

from transfer_hub.watcher_notify import (
    WatcherNotifyError,
    WatcherWebhook,
    build_ready_payload,
    build_test_payload,
)


def test_plain_http_webhook_must_stay_private(tmp_path: Path):
    secret = tmp_path / "secret"
    secret.write_text("x" * 32)
    WatcherWebhook("http://100.100.100.100:8651/webhooks/p20", secret)
    with pytest.raises(WatcherNotifyError):
        WatcherWebhook("http://8.8.8.8:8651/webhooks/p20", secret)


def test_payloads_are_explicit_about_test_and_ready():
    test = build_test_payload("http://100.84.55.11:17610")
    assert test["test"] is True
    assert test["event_type"] == "p20.watcher.test"
    assert "这是一次测试任务" in test["message"]

    ready = build_ready_payload(
        {"id": "demo", "name": "Demo Watch"},
        {
            "id": "abc", "job_number": 12, "state": "READY", "title": "Demo v2",
            "options": {"watch_version": "v2", "watch_run_id": 9},
        },
        "http://100.84.55.11:17610",
    )
    assert ready["test"] is False
    assert ready["event_type"] == "p20.watcher.ready"
    assert ready["job_number_label"] == "P20-000012"
    assert "Demo v2" in ready["message"]


def test_webhook_uses_timestamped_hmac_v2(tmp_path: Path, monkeypatch):
    secret_value = "s" * 40
    secret = tmp_path / "secret"
    secret.write_text(secret_value)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"status": "delivered", "target": "feishu"}).encode()

    def fake_urlopen(req, timeout=0):
        captured["req"] = req
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    webhook = WatcherWebhook("http://100.100.100.100:8651/webhooks/p20", secret)
    result = webhook.send({"event_type": "p20.watcher.test", "message": "hello"})
    assert result["status"] == "delivered"

    req = captured["req"]
    headers = {k.lower(): v for k, v in req.header_items()}
    timestamp = headers["x-webhook-timestamp"]
    expected = hmac.new(
        secret_value.encode(), timestamp.encode() + b"." + req.data, hashlib.sha256,
    ).hexdigest()
    assert headers["x-webhook-signature-v2"] == expected
    assert captured["timeout"] == 10


def test_ready_notifier_does_not_lose_older_ready_job(tmp_path: Path):
    from transfer_hub.watcher_notify import ReadyNotifier

    class FakeStore:
        def __init__(self):
            self.events = {}

        def list_watch_tasks(self):
            return [{"id": "demo", "name": "Demo", "last_job_id": "new"}]

        def list_jobs(self, _limit=100):
            base = {"state": "READY", "title": "Ready", "job_number": 1,
                    "options": {"watch_notify": True, "watcher_id": "demo", "watch_version": "v1"}}
            return [{**base, "id": "new"}, {**base, "id": "older", "job_number": 2}]

        def recent_events(self, job_id, _limit, _event_type=None):
            return self.events.get(job_id, [])

        def event(self, job_id, event_type, data):
            self.events.setdefault(job_id, []).append({"type": event_type, "data": data})

    class FakeWebhook:
        public_url = "http://100.84.55.11:17610"

        def __init__(self):
            self.sent = []

        def configured(self):
            return True

        def send(self, payload):
            self.sent.append(payload)
            return {"request_id": "test", "status": "delivered", "target": "feishu"}

    store = FakeStore()
    webhook = FakeWebhook()
    notifier = ReadyNotifier(store, webhook)
    first = notifier.scan()
    second = notifier.scan()

    assert {item["job_id"] for item in first} == {"new", "older"}
    assert len(webhook.sent) == 2
    assert second == []
