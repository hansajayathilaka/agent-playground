"""SecretStore with AES-256-GCM and a master key from the environment.

Ciphertext layout: b"\\x01" (format version) + 12-byte nonce + ciphertext+tag.
The record context (for example "credential-set:<id>") is the associated
data, so a ciphertext moved to another record fails to decrypt.
"""

from __future__ import annotations

import base64
import binascii
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"


def parse_master_key(value: str) -> bytes:
    """A base64 (standard or urlsafe) encoding of exactly 32 random bytes."""
    if not value:
        raise ValueError("SECRETS_KEY must be set (base64 of 32 random bytes)")
    try:
        raw = base64.urlsafe_b64decode(value.replace("+", "-").replace("/", "_") + "==")
    except (binascii.Error, ValueError) as e:
        raise ValueError("SECRETS_KEY is not valid base64") from e
    if len(raw) != 32:
        raise ValueError("SECRETS_KEY must decode to 32 bytes")
    return raw


def generate_master_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


class AesGcmSecretStore:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes, *, context: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return _VERSION + nonce + self._aead.encrypt(nonce, plaintext, context.encode())

    def decrypt(self, ciphertext: bytes, *, context: str) -> bytes:
        if len(ciphertext) < 29 or ciphertext[:1] != _VERSION:
            raise ValueError("unrecognized secret format")
        nonce, body = ciphertext[1:13], ciphertext[13:]
        try:
            return self._aead.decrypt(nonce, body, context.encode())
        except InvalidTag as e:
            raise ValueError("secret could not be decrypted (wrong key or context)") from e
