"""Encrypts exchange API credentials before they touch the database (Fernet: AES-128-CBC + HMAC-SHA256).

The key comes from VAULT_KEY in .env or, if that is empty, from a key file created next to the database on first
use. Someone who copies only the database cannot read the credentials; keep the key file (or VAULT_KEY) private.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(RuntimeError):
    """The stored credentials can't be decrypted (the vault key was changed or lost)."""


class Vault:
    def __init__(self, key: str | bytes):
        self._fernet = Fernet(key)

    @classmethod
    def open(cls, key: str | None, key_file: str | Path) -> Vault:
        if key:
            return cls(key)
        path = Path(key_file)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # readable by the bot's user only
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        return cls(path.read_bytes().strip())

    def seal(self, data: dict[str, str]) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def unseal(self, token: str) -> dict[str, str]:
        try:
            return json.loads(self._fernet.decrypt(token.encode()))
        except (InvalidToken, ValueError) as exc:
            raise VaultError("stored keys can't be decrypted — reconnect the exchange") from exc
