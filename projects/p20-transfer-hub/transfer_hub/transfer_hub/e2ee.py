from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"P20E2E1\n"
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
_MAX_HEADER = 1024 * 1024


class E2EEError(RuntimeError):
    pass


@dataclass(frozen=True)
class EncryptResult:
    ciphertext_path: Path
    ciphertext_size: int
    ciphertext_sha256: str
    plaintext_size: int
    chunks: int


@dataclass(frozen=True)
class DecryptResult:
    output_path: Path
    filename: str
    plaintext_size: int
    plaintext_sha256: str


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _atomic_json(path: Path, data: dict[str, Any], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def generate_receiver_keypair(private_path: Path, public_path: Path) -> None:
    private_key = X25519PrivateKey.generate()
    private_raw = private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public_raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    _atomic_json(private_path, {"type": "p20-x25519-private-v1", "key": _b64(private_raw)}, 0o600)
    _atomic_json(public_path, {"type": "p20-x25519-public-v1", "key": _b64(public_raw)}, 0o644)


def _read_key(path: Path, expected_type: str) -> bytes:
    try:
        data = json.loads(path.read_text())
        if data.get("type") != expected_type:
            raise ValueError("wrong key type")
        raw = _unb64(str(data["key"]))
    except Exception as exc:
        raise E2EEError(f"invalid key file: {path}") from exc
    if len(raw) != 32:
        raise E2EEError("invalid X25519 key length")
    return raw


def load_public_key(path: Path) -> X25519PublicKey:
    return X25519PublicKey.from_public_bytes(_read_key(path, "p20-x25519-public-v1"))


def load_private_key(path: Path) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(_read_key(path, "p20-x25519-private-v1"))


def _derive_kek(shared: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"p20-cmcloud-e2ee-v1").derive(shared)


def _chunk_aad(plaintext_size: int, index: int) -> bytes:
    return MAGIC + b":chunk:" + struct.pack(">QI", plaintext_size, index)


def encrypt_file(source: Path, destination: Path, recipient_public_key: Path, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> EncryptResult:
    if chunk_size < 64 * 1024:
        raise E2EEError("chunk size is too small")
    source = source.resolve()
    plaintext_size = source.stat().st_size
    chunk_count = max(1, (plaintext_size + chunk_size - 1) // chunk_size)
    if chunk_count >= 2**32:
        raise E2EEError("too many encryption chunks")

    recipient = load_public_key(recipient_public_key)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_raw = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    salt = os.urandom(16)
    kek = _derive_kek(ephemeral.exchange(recipient), salt)
    dek = os.urandom(32)
    wrap_nonce = os.urandom(12)
    wrapped_dek = AESGCM(kek).encrypt(wrap_nonce, dek, MAGIC + b":keywrap")

    metadata = {
        "filename": source.name,
        "mime": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
    }
    meta_nonce = os.urandom(12)
    encrypted_meta = AESGCM(dek).encrypt(meta_nonce, json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode(), MAGIC + b":meta")
    nonce_prefix = os.urandom(8)

    header = {
        "v": 1,
        "alg": "A256GCM-chunked",
        "chunk_size": chunk_size,
        "plaintext_size": plaintext_size,
        "chunks": chunk_count,
        "nonce_prefix": _b64(nonce_prefix),
        "key_wrap": {
            "alg": "X25519-HKDF-SHA256+A256GCM",
            "ephemeral_public": _b64(ephemeral_raw),
            "salt": _b64(salt),
            "nonce": _b64(wrap_nonce),
            "wrapped_dek": _b64(wrapped_dek),
        },
        "meta_nonce": _b64(meta_nonce),
        "encrypted_meta": _b64(encrypted_meta),
    }
    header_raw = json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    if len(header_raw) > _MAX_HEADER:
        raise E2EEError("E2EE header is too large")

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    hasher = hashlib.sha256()
    aead = AESGCM(dek)
    try:
        with source.open("rb") as src, tmp.open("wb") as dst:
            prefix = MAGIC + struct.pack(">I", len(header_raw)) + header_raw
            dst.write(prefix)
            hasher.update(prefix)
            for index in range(chunk_count):
                plaintext = src.read(chunk_size)
                if plaintext_size == 0:
                    plaintext = b""
                nonce = nonce_prefix + struct.pack(">I", index)
                encrypted = aead.encrypt(nonce, plaintext, _chunk_aad(plaintext_size, index))
                dst.write(encrypted)
                hasher.update(encrypted)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, destination)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    return EncryptResult(destination, destination.stat().st_size, hasher.hexdigest(), plaintext_size, chunk_count)


def _parse_header(handle: BinaryIO) -> dict[str, Any]:
    if handle.read(len(MAGIC)) != MAGIC:
        raise E2EEError("not a P20 E2EE object")
    raw_len = handle.read(4)
    if len(raw_len) != 4:
        raise E2EEError("truncated E2EE header")
    length = struct.unpack(">I", raw_len)[0]
    if length <= 0 or length > _MAX_HEADER:
        raise E2EEError("invalid E2EE header length")
    try:
        data = json.loads(handle.read(length))
    except Exception as exc:
        raise E2EEError("invalid E2EE header") from exc
    if data.get("v") != 1 or data.get("alg") != "A256GCM-chunked":
        raise E2EEError("unsupported E2EE format")
    return data


def decrypt_file(source: Path, destination: Path | None, recipient_private_key: Path) -> DecryptResult:
    source = source.resolve()
    private = load_private_key(recipient_private_key)
    with source.open("rb") as src:
        header = _parse_header(src)
        try:
            kw = header["key_wrap"]
            ephemeral = X25519PublicKey.from_public_bytes(_unb64(kw["ephemeral_public"]))
            kek = _derive_kek(private.exchange(ephemeral), _unb64(kw["salt"]))
            dek = AESGCM(kek).decrypt(_unb64(kw["nonce"]), _unb64(kw["wrapped_dek"]), MAGIC + b":keywrap")
            metadata_raw = AESGCM(dek).decrypt(_unb64(header["meta_nonce"]), _unb64(header["encrypted_meta"]), MAGIC + b":meta")
            metadata = json.loads(metadata_raw)
        except (KeyError, ValueError, InvalidTag, json.JSONDecodeError) as exc:
            raise E2EEError("unable to unwrap E2EE object key") from exc

        filename = Path(str(metadata.get("filename") or "p20-decrypted.bin")).name
        if destination is None:
            destination = source.with_name(filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        plaintext_size = int(header["plaintext_size"])
        chunk_size = int(header["chunk_size"])
        chunks = int(header["chunks"])
        nonce_prefix = _unb64(header["nonce_prefix"])
        hasher = hashlib.sha256()
        remaining = plaintext_size
        try:
            with tmp.open("wb") as dst:
                for index in range(chunks):
                    plain_len = min(chunk_size, remaining) if plaintext_size else 0
                    cipher_len = plain_len + 16
                    encrypted = src.read(cipher_len)
                    if len(encrypted) != cipher_len:
                        raise E2EEError("truncated E2EE ciphertext")
                    nonce = nonce_prefix + struct.pack(">I", index)
                    try:
                        plaintext = AESGCM(dek).decrypt(nonce, encrypted, _chunk_aad(plaintext_size, index))
                    except InvalidTag as exc:
                        raise E2EEError(f"E2EE integrity check failed at chunk {index}") from exc
                    dst.write(plaintext)
                    hasher.update(plaintext)
                    remaining -= len(plaintext)
                if src.read(1):
                    raise E2EEError("unexpected trailing E2EE data")
                dst.flush()
                os.fsync(dst.fileno())
            if remaining != 0:
                raise E2EEError("E2EE plaintext size mismatch")
            os.replace(tmp, destination)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return DecryptResult(destination, filename, plaintext_size, hasher.hexdigest())
