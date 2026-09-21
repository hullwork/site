"""AES-GCM encryption for app secrets at rest.

The AAD binds a ciphertext to the exact row it was written for
(``appEnvId + "\\0" + name``), so a ciphertext copied between rows -- by a bug, a
backup restore, or a future bulk-migration script -- fails to decrypt instead of
silently decrypting as someone else's secret.

Ciphertext format is versioned so the scheme can change without a data migration:
``b"\\x01" + nonce(12) + aesgcm(nonce, plaintext, aad)``. Version 1 is the only one
today; unknown versions are refused rather than guessed at.
"""
from __future__ import annotations

import os
from os import getenv
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_NONCE_LEN = 12
_KEY_LEN = 32


class SecretBoxError(Exception):
    pass


def _aad(app_env_id: str, name: str) -> bytes:
    return f"{app_env_id}\0{name}".encode("utf-8")


def load_key(path: str) -> bytes:
    try:
        key = Path(path).read_bytes()
    except OSError as exc:
        raise RuntimeError(f"broker secret key file is unreadable: {path}") from exc
    if len(key) != _KEY_LEN:
        raise RuntimeError(
            f"broker secret key must be exactly {_KEY_LEN} bytes, got {len(key)}"
        )
    return key


def key_from_env() -> bytes:
    path = (getenv("HULL_BROKER_SECRET_KEY_FILE", "") or "").strip()
    if not path:
        raise RuntimeError("HULL_BROKER_SECRET_KEY_FILE is required")
    return load_key(path)


def encrypt(key: bytes, app_env_id: str, name: str, plaintext: str) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _aad(app_env_id, name))
    return _VERSION + nonce + ciphertext


def decrypt(key: bytes, app_env_id: str, name: str, blob: bytes) -> str:
    if not blob or blob[:1] != _VERSION:
        raise SecretBoxError("secret ciphertext has an unsupported version")
    nonce = blob[1:1 + _NONCE_LEN]
    ciphertext = blob[1 + _NONCE_LEN:]
    if len(nonce) != _NONCE_LEN or not ciphertext:
        raise SecretBoxError("secret ciphertext is truncated")
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(app_env_id, name))
    except InvalidTag as exc:
        raise SecretBoxError("secret ciphertext does not decrypt with this key and AAD") from exc
    return plaintext.decode("utf-8")
