"""Regenerate the OddsPortal feed `.dat` fixtures from the decrypted reference.

Run with: ``uv run python tests/fixtures/_gen_oddsportal_feed.py``

The `.dat` fixtures are REAL encrypted envelopes — the decrypted reference JSON
is encrypted with the SAME static public-bundle key + envelope format the live
feed uses (``base64("{ct_b64}:{iv_hex}")``, AES-256-CBC, optional gzip), so the
tests exercise the full decrypt path, not a stubbed blob. Caller of the output
files: ``tests/test_oddsportal_json.py`` (``_FEED`` / ``_FEED_GZIP``).

This generator is the ONLY place that needs the encrypt direction; the shipped
module is decrypt/GET-only (READ-ONLY market-data safety rule).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

# Static decrypt constants, mirrored from app/ingestion/oddsportal_json.py.
_KDF_PASSPHRASE = "J*8sQ!p$7aD_fR2yW@gHn*3bVp#sAdLd_k"
_KDF_SALT = b"5b9a8f2c3e6d1a4b7c8e9d0f1a2b3c4d"
_KDF_ITERATIONS = 1000
_KDF_DKLEN = 32
# Deterministic 16-byte IV so the fixtures are reproducible (the live feed uses
# a random IV per body; the decrypt does not care which IV produced the body).
_IV = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")

_HERE = Path(__file__).parent
_DECRYPTED = _HERE / "oddsportal_feed_KhgvzGjJ.decrypted.json"
_FEED = _HERE / "oddsportal_feed_KhgvzGjJ.dat"
_FEED_GZIP = _HERE / "oddsportal_feed_KhgvzGjJ_gzip.dat"


def _key() -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", _KDF_PASSPHRASE.encode(), _KDF_SALT, _KDF_ITERATIONS, _KDF_DKLEN
    )


def _encrypt(plaintext: bytes) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_key()), modes.CBC(_IV)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    ct_b64 = base64.b64encode(ciphertext).decode()
    outer = f"{ct_b64}:{_IV.hex()}"
    return base64.b64encode(outer.encode()).decode()


def main() -> None:
    plaintext = _DECRYPTED.read_bytes()
    _FEED.write_text(_encrypt(plaintext))
    _FEED_GZIP.write_text(_encrypt(gzip.compress(plaintext)))
    print(f"wrote {_FEED.name} ({_FEED.stat().st_size} B)")
    print(f"wrote {_FEED_GZIP.name} ({_FEED_GZIP.stat().st_size} B)")


if __name__ == "__main__":
    main()
