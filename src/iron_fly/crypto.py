from __future__ import annotations

import json

from cryptography.fernet import Fernet


def encrypt_secrets(key: str, payload: dict) -> bytes:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return Fernet(key.encode()).encrypt(raw)


def decrypt_secrets(key: str, blob: bytes) -> dict:
    raw = Fernet(key.encode()).decrypt(blob)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("credential payload is not an object")
    return data
