"""At-rest encryption of the wdgwars keys (Fernet = AES-128-CBC + HMAC).
Master key from env WARROOM_MASTER_KEY, otherwise from data/master.key (created on
first start, 0600). Without the master key no stored key can be decrypted
— so backing up master.key is part of the DB backup."""
import os

from cryptography.fernet import Fernet

from . import config

_fernet: Fernet | None = None


def _load() -> Fernet:
    global _fernet
    if _fernet:
        return _fernet
    key = os.environ.get("WARROOM_MASTER_KEY")
    if not key:
        p = config.MASTER_KEY_PATH
        if p.exists():
            key = p.read_text(encoding="utf-8").strip()
        else:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            p.write_text(key, encoding="utf-8")
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return _load().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _load().decrypt(token.encode()).decode()
