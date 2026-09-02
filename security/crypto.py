from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

APP_ROOT = Path(__file__).resolve().parents[1]
KEY_PATH = APP_ROOT / ".encryption_key"
ENV_PATH = APP_ROOT / ".env"

ENC_PREFIX = "enc:"


class CryptoError(Exception):
    pass


def generate_key(force: bool = False) -> Path:
    if KEY_PATH.exists() and not force:
        raise CryptoError(f"{KEY_PATH} already exists — pass --force to overwrite it.")
    KEY_PATH.write_bytes(Fernet.generate_key())
    KEY_PATH.chmod(0o600)
    return KEY_PATH


def _get_key() -> bytes:
    env_key = os.getenv("ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes().strip()
    raise CryptoError(
        "No encryption key found. Set the ENCRYPTION_KEY environment variable, "
        "or run: python security/crypto.py --generate-key"
    )


def encrypt_value(plaintext: str) -> str:
    f = Fernet(_get_key())
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(token: str) -> str:
    f = Fernet(_get_key())
    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise CryptoError(
            "Could not decrypt a value from .env — wrong ENCRYPTION_KEY/.encryption_key, "
            "or the value is corrupted."
        ) from e


def resolve_env_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw.startswith(ENC_PREFIX):
        return decrypt_value(raw[len(ENC_PREFIX):])
    return raw


# ── .env file rewriting ──────────────────────────────────────────

def _parse_env_lines(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            pairs.append((m.group(1), m.group(2)))
    return pairs


def encrypt_env_fields(fields: list[str], env_path: Path = ENV_PATH) -> list[str]:
    if not env_path.exists():
        raise CryptoError(f"{env_path} does not exist.")
    text = env_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed = []

    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key not in fields:
            continue
        if value.startswith(ENC_PREFIX):
            continue  # already encrypted
        if value == "":
            continue  # nothing to encrypt
        encrypted = ENC_PREFIX + encrypt_value(value)
        lines[i] = f"{key}={encrypted}"
        changed.append(key)

    if changed:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Encrypt/decrypt .env credentials")
    parser.add_argument("--generate-key", action="store_true", help="Create .encryption_key")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing key file")
    parser.add_argument("--encrypt-env", action="store_true", help="Encrypt fields in .env in place")
    parser.add_argument(
        "--fields", default="USERNAME,PASSWORD",
        help="Comma-separated .env keys to encrypt (default: USERNAME,PASSWORD)",
    )
    args = parser.parse_args()

    if args.generate_key:
        path = generate_key(force=args.force)
        print(f"Generated encryption key at {path}")
        print("Keep this file safe and out of version control.")

    if args.encrypt_env:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        changed = encrypt_env_fields(fields)
        if changed:
            print(f"Encrypted in .env: {', '.join(changed)}")
        else:
            print("Nothing to encrypt (fields missing, empty, or already encrypted).")

    if not any([args.generate_key, args.encrypt_env]):
        parser.print_help()
