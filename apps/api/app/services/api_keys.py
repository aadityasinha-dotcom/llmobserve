"""Minting API keys. One format, used by the CLI and by the dashboard's
self-serve endpoint alike, so a key issued either way looks and verifies the same."""

import secrets
from dataclasses import dataclass

from app.deps import hash_api_key

KEY_PREFIX = "llmo_sk_"
# Enough of the key to tell two apart in a list; far too little to use.
DISPLAY_PREFIX_LENGTH = 16


@dataclass(frozen=True)
class NewApiKey:
    raw: str
    key_hash: str
    display_prefix: str


def generate_api_key() -> NewApiKey:
    # 32 bytes from the OS CSPRNG: 256 bits, unguessable, so an unsalted
    # SHA-256 is a sound storage format (see app.deps.hash_api_key).
    # token_urlsafe never emits "." - which is what keeps a key from ever
    # being mistaken for a session JWT.
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return NewApiKey(
        raw=raw, key_hash=hash_api_key(raw), display_prefix=raw[:DISPLAY_PREFIX_LENGTH]
    )
