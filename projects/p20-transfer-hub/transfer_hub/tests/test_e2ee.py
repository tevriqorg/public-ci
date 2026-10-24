import hashlib
import json
from pathlib import Path

import pytest

import p20_cmcloud_e2ee as client
from transfer_hub.e2ee import E2EEError, decrypt_file, encrypt_file, generate_receiver_keypair


def _keys(tmp_path: Path, prefix: str = "receiver"):
    private = tmp_path / f"{prefix}.private.json"
    public = tmp_path / f"{prefix}.public.json"
    generate_receiver_keypair(private, public)
    return private, public


def test_e2ee_round_trip_and_filename_is_not_plaintext(tmp_path: Path):
    private, public = _keys(tmp_path)
    source = tmp_path / "private-family-video.mov"
    payload = (b"camera-frame-" * 20000) + b"tail"
    source.write_bytes(payload)
    cipher = tmp_path / "object.blob"

    enc = encrypt_file(source, cipher, public, chunk_size=64 * 1024)
    assert enc.plaintext_size == len(payload)
    assert enc.ciphertext_sha256 == hashlib.sha256(cipher.read_bytes()).hexdigest()
    assert source.name.encode() not in cipher.read_bytes()

    output = tmp_path / "restored.mov"
    dec = decrypt_file(cipher, output, private)
    assert output.read_bytes() == payload
    assert dec.filename == source.name
    assert dec.plaintext_sha256 == hashlib.sha256(payload).hexdigest()
    assert (private.stat().st_mode & 0o777) == 0o600


def test_wrong_receiver_key_cannot_decrypt(tmp_path: Path):
    private, public = _keys(tmp_path, "one")
    wrong_private, _ = _keys(tmp_path, "two")
    source = tmp_path / "secret.txt"
    source.write_text("top secret")
    cipher = tmp_path / "secret.blob"
    encrypt_file(source, cipher, public, chunk_size=64 * 1024)

    with pytest.raises(E2EEError, match="unwrap"):
        decrypt_file(cipher, tmp_path / "wrong.txt", wrong_private)


def test_ciphertext_tamper_is_detected(tmp_path: Path):
    private, public = _keys(tmp_path)
    source = tmp_path / "secret.bin"
    source.write_bytes(b"x" * 200000)
    cipher = tmp_path / "secret.blob"
    encrypt_file(source, cipher, public, chunk_size=64 * 1024)
    raw = bytearray(cipher.read_bytes())
    raw[-1] ^= 0x01
    cipher.write_bytes(raw)

    with pytest.raises(E2EEError, match="integrity"):
        decrypt_file(cipher, tmp_path / "bad.bin", private)


class FakeBroker:
    def __init__(self):
        self.created = 0
        self.issued = []
        self.completed = []

    def create_upload(self, size, digest):
        self.created += 1
        return {"session_id": "a" * 32, "part_size": 4, "part_count": 2, "parts": [{"part_number": 1, "upload_url": "https://eos.invalid/one"}], "exist": False, "rapid_upload": False}

    def issue_parts(self, session_id, numbers):
        self.issued.append(list(numbers))
        return {"parts": [{"part_number": n, "upload_url": f"https://eos.invalid/{n}"} for n in numbers]}

    def complete(self, session_id):
        self.completed.append(session_id)
        return {"state": "completed"}


def test_upload_state_supports_resume_without_persisting_signed_urls(tmp_path: Path, monkeypatch):
    cipher = tmp_path / "cipher.blob"
    cipher.write_bytes(b"12345678")
    state_path = tmp_path / "upload.json"
    broker = FakeBroker()
    puts = []

    def fake_put(url, source, offset, length, timeout=180.0):
        puts.append((url, offset, length))

    monkeypatch.setattr(client, "_put_part", fake_put)
    state = client.upload_ciphertext(cipher, broker, state_path)
    assert state["state"] == "completed"
    assert len(puts) == 2
    persisted = state_path.read_text()
    assert "eos.invalid" not in persisted
    assert json.loads(persisted)["completed_parts"] == [1, 2]
    assert broker.issued == [[2]]

    client.upload_ciphertext(cipher, broker, state_path)
    assert broker.created == 1
    assert len(puts) == 2
