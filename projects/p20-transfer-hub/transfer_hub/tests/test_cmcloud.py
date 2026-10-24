import base64
import json
from pathlib import Path

from transfer_hub.cmcloud import CMCloudBroker, CMCloudProvider, decode_authorization


def make_auth(account="13800138000", expiry=4102444800000, token_prefix="token"):
    token = f"{token_prefix}|b|c|{expiry}|e"
    return base64.b64encode(f"pc:{account}:{token}".encode()).decode()


def test_decode_authorization_extracts_account_and_expiry():
    info = decode_authorization(make_auth(expiry=1893456000000))
    assert info.prefix == "pc"
    assert info.account == "13800138000"
    assert info.expires_ms == 1893456000000
    assert "token" in info.token


class FakeProvider:
    def __init__(self):
        self.calls = []

    def create_upload(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"file_id": "f1", "file_name": kwargs["name"], "upload_id": "u1", "exist": False, "rapid_upload": False,
                "parts": [{"part_number": 1, "upload_url": "https://eos.example/part?sig=secret"}], "part_size": 100, "part_count": 2}

    def get_upload_urls(self, **kwargs):
        self.calls.append(("parts", kwargs))
        return [{"part_number": n, "upload_url": f"https://eos.example/{n}?sig=secret"} for n in kwargs["part_numbers"]]

    def complete(self, **kwargs): self.calls.append(("complete", kwargs))
    def download(self, file_id):
        self.calls.append(("download", file_id))
        return {"url": "https://eos.example/object?sig=secret", "expires_at": 123, "host": "eos.example"}
    def trash(self, file_id): self.calls.append(("trash", file_id))


def test_broker_e2ee_uses_opaque_provider_name_and_never_persists_signed_urls(tmp_path: Path):
    provider = FakeProvider()
    broker = CMCloudBroker(provider, tmp_path)
    created = broker.create_upload(size=200, sha256="a" * 64, name="family-video.mov", encryption="e2ee", opaque_metadata="ciphertext-manifest")
    assert created["provider_name"].startswith("p20-")
    assert "family-video" not in created["provider_name"]
    assert created["parts"][0]["upload_url"].startswith("https://")
    persisted = json.loads((tmp_path / "cmcloud" / "sessions" / f"{created['session_id']}.json").read_text())
    assert "parts" not in persisted
    assert "upload_url" not in json.dumps(persisted)
    assert persisted["opaque_metadata"] == "ciphertext-manifest"
    more = broker.issue_parts(created["session_id"], [2])
    assert more["parts"][0]["part_number"] == 2
    broker.complete(created["session_id"])
    cap = broker.download(created["session_id"])
    assert cap["encryption"] == "e2ee"
    assert cap["url"].startswith("https://")
    broker.trash(created["session_id"])


def test_provider_status_is_redacted(tmp_path: Path):
    auth = tmp_path / "auth"
    auth.write_text(make_auth())
    provider = CMCloudProvider(auth, tmp_path / "state.json")
    status = provider.status()
    assert status["configured"] is True
    assert status["account"] == "138****8000"
    assert "authorization" not in json.dumps(status).lower()
    assert "token" not in json.dumps(status).lower()


def test_refresh_replaces_authorization_without_exposing_token(tmp_path: Path):
    auth = tmp_path / "auth"
    auth.write_text(make_auth(expiry=1, token_prefix="old"))

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b"<root><return>0</return><token>new|b|c|4102444800000|e</token></root>"

    seen = {}
    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["content_type"] = req.headers.get("Content-type")
        return Response()

    provider = CMCloudProvider(auth, tmp_path / "state.json", opener=opener)
    assert provider.refresh(force=True) is True
    assert seen["url"].endswith("authTokenRefresh.do")
    assert decode_authorization(auth.read_text().strip()).token.startswith("new|")
    assert (auth.stat().st_mode & 0o777) == 0o600
