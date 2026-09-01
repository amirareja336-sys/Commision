from __future__ import annotations

import base64
import hashlib
import os
from typing import Mapping

from cryptography.fernet import Fernet


def _derive_key(key: str) -> bytes:
    if not key:
        raise ValueError("Encryption key cannot be empty")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plaintext: str, key: str) -> str:
    """Encrypt a plaintext secret with a local key."""
    if plaintext is None:
        raise ValueError("Plaintext secret cannot be None")
    fernet = Fernet(_derive_key(key))
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str, key: str) -> str:
    """Decrypt a secret encrypted with encrypt_secret()."""
    if not ciphertext:
        raise ValueError("Ciphertext cannot be empty")
    fernet = Fernet(_derive_key(key))
    return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def get_secret(
    name: str,
    *,
    default: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return a raw secret from the environment.

    Supports either direct plaintext values (e.g. PASSWORD=...)
    or encrypted values stored as PASSWORD_ENCRYPTED plus PASSWORD_KEY.
    """
    values = env if env is not None else os.environ

    direct = values.get(name)
    if direct:
        return direct

    encrypted_name = f"{name}_ENCRYPTED"
    key_name = f"{name}_KEY"
    encrypted = values.get(encrypted_name)
    key = values.get(key_name) or values.get("APP_SECRET_KEY")

    if encrypted and key:
        try:
            return decrypt_secret(encrypted, key)
        except Exception as exc:  # pragma: no cover - defensive path
            raise RuntimeError(f"Could not decrypt {encrypted_name}") from exc

    return default
