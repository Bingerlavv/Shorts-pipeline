"""Шифрование учётных данных площадок."""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


class CredentialsError(RuntimeError):
    pass


@lru_cache
def _fernet() -> Fernet:
    key = settings.secret_key.strip()
    if not key:
        raise CredentialsError(
            "SHORTS_SECRET_KEY не задан. Сгенерируй: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        # Пользователь мог вписать произвольную строку — выводим из неё валидный ключ.
        derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        return Fernet(derived)


def encrypt_json(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_json(token: str) -> dict[str, Any]:
    if not token:
        return {}
    try:
        raw = _fernet().decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise CredentialsError(
            "не удалось расшифровать учётные данные — SHORTS_SECRET_KEY изменился, "
            "аккаунт нужно подключить заново"
        ) from exc
    return json.loads(raw.decode("utf-8"))
