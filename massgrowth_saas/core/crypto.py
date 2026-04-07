"""
core/crypto.py — шифрование/дешифрование паролей через Fernet.

Ключ выводится из SECRET_KEY в .env через SHA-256.
Fernet обеспечивает симметричное шифрование (AES-128-CBC + HMAC-SHA256).

Использование:
    from core.crypto import encrypt_password, decrypt_password

    stored = encrypt_password("my_instagram_pass")   # → зашифрованная строка
    plain  = decrypt_password(stored)                # → "my_instagram_pass"
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _build_fernet() -> Fernet:
    """
    Создаёт Fernet-экземпляр из SECRET_KEY окружения.

    Fernet требует ровно 32 байта в URL-safe base64.
    Мы получаем их через SHA-256 от SECRET_KEY — детерминированно,
    без хранения самого ключа в БД.

    Raises:
        RuntimeError: Если SECRET_KEY не задан или слишком короткий.
    """
    secret = os.getenv("SECRET_KEY", "")
    if len(secret) < 16:
        raise RuntimeError(
            "SECRET_KEY must be at least 16 characters. "
            "Set it in .env: SECRET_KEY=your-random-32-char-string"
        )
    # SHA-256 → 32 байта → URL-safe base64 = валидный Fernet ключ
    raw_key = hashlib.sha256(secret.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(raw_key)
    return Fernet(fernet_key)


def encrypt_password(plain_text: str) -> str:
    """
    Шифрует пароль перед сохранением в БД.

    Args:
        plain_text: Открытый пароль.

    Returns:
        Зашифрованная строка (URL-safe, хранится в поле password_encrypted).

    Raises:
        RuntimeError: Если SECRET_KEY не настроен.
    """
    fernet = _build_fernet()
    return fernet.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_password(encrypted: str) -> str:
    """
    Расшифровывает пароль для использования при логине.

    Args:
        encrypted: Зашифрованная строка из БД.

    Returns:
        Открытый пароль.

    Raises:
        RuntimeError: Если расшифровка не удалась (ключ изменился или данные повреждены).
    """
    fernet = _build_fernet()
    try:
        return fernet.decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Failed to decrypt password — SECRET_KEY may have changed "
            "or the stored value is corrupted."
        ) from e
