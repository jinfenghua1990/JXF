"""登录鉴权：邮箱+密码账号 + HMAC 签名 token（仅用标准库，无额外依赖）。

设计取舍（面向小团队自托管 / 外网暴露前必开）：
- 账号以「邮箱」标识，密码用 PBKDF2-HMAC-SHA256 加盐哈希存储（不存明文）。
- token 无状态、30 天有效，载荷里带邮箱，便于后续审计是谁操作的。
- 接口分两层：require_auth（登录即可用）与 require_admin（仅管理员，用于账号管理）。
- 若系统里没有任何账号（且无法从环境变量播种），则任何人拿不到 token，
  所有数据接口天然 401/503，绝不裸奔。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Header, HTTPException
from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import User

TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 天

# 本系统仅支持「账号 + 密码」登录，不依赖邮件 / 验证码。

_FALLBACK_SECRET = "insecure-dev-secret-change-me"


def _dev_identity() -> str:
    """开发期免登录时使用的虚拟账号标识。"""
    return (settings.admin_email or "dev@local").strip().lower()


def _secret() -> str:
    return settings.app_secret or _FALLBACK_SECRET


# ---------------------------------------------------------------- 密码哈希


def hash_password(plain: str) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希，返回可存储的字符串。"""
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), bytes.fromhex(salt), 100_000).hex()
    return f"pbkdf2$sha256$100000${salt}${dk}"


def verify_user_password(plain: str, stored: str) -> bool:
    try:
        algo, _sh, iters, salt, dk = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    try:
        calc = hashlib.pbkdf2_hmac(
            "sha256", plain.encode("utf-8"), bytes.fromhex(salt), int(iters)
        ).hex()
    except Exception:
        return False
    return hmac.compare_digest(calc, dk)


# ---------------------------------------------------------------- token


def make_token(email: str) -> str:
    exp = int(time.time()) + TOKEN_TTL_SECONDS
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp, "email": email}).encode())
        .decode()
        .rstrip("=")
    )
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: Optional[str]) -> Optional[str]:
    """返回 token 中的邮箱；无效/过期返回 None。"""
    if not token:
        return None
    try:
        payload, sig = token.split(".")
    except ValueError:
        return None
    expected = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        raw = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(raw))
    except Exception:
        return None
    if not data.get("exp", 0) > int(time.time()):
        return None
    return data.get("email")


# ---------------------------------------------------------------- 依赖


def require_auth(authorization: str = Header(default="")) -> str:
    """登录即可用的依赖；返回当前账号邮箱。"""
    if settings.disable_auth:
        # 开发期免登录：跳过 token 校验，直接返回开发身份。
        return _dev_identity()
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    email = verify_token(token)
    if not email:
        raise HTTPException(401, "未登录或登录已过期，请重新登录")
    return email


def require_admin(authorization: str = Header(default="")) -> User:
    """仅管理员可用的依赖；返回 User 对象。"""
    email = require_auth(authorization)
    if settings.disable_auth:
        # 开发期免登录：返回虚拟管理员身份，避免依赖数据库里的真实账号。
        return User(id=-1, email=email, is_admin=True, is_active=True)
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
    finally:
        db.close()
    if user is None or not user.is_active or not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user
