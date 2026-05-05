"""Kalshi RSA-PSS-256 request signing.

The signed string is `<timestamp_ms><method><path>` where path excludes the host
and query string. Signature uses PSS padding with SHA-256, MGF1(SHA-256), salt
length equal to the digest length, base64-encoded.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


class KalshiSigner:
    def __init__(self, key_id: str, private_key_path: Path):
        self.key_id = key_id
        self._private_key = self._load_key(private_key_path)

    @staticmethod
    def _load_key(path: Path) -> RSAPrivateKey:
        with open(path, "rb") as f:
            data = f.read()
        key = serialization.load_pem_private_key(data, password=None)
        if not isinstance(key, RSAPrivateKey):
            raise ValueError("KALSHI_PRIVATE_KEY_PATH must be an RSA PEM private key")
        return key

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        message = f"{ts_ms}{method.upper()}{path}".encode()
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
