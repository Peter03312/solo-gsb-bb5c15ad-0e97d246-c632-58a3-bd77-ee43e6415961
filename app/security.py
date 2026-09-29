"""Password hashing (PBKDF2, stdlib only) and auth dependencies."""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from .db import connect

_ROLES = {"curator", "translator", "reviewer"}


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def get_db() -> Any:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any] | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    row = conn.execute(
        "SELECT id, username, display_name, role FROM users WHERE id = ?", (uid,)
    ).fetchone()
    return dict(row) if row else None


def require_login(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "not_authenticated",
                                    "message": "请先登录。"})
    return user


def require_roles(*roles: str):
    def checker(user: dict[str, Any] = Depends(require_login)) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail={"code": "forbidden",
                                        "message": "当前账号无权执行该操作。"})
        return user
    return checker
