"""可选访问密码（Docker / 网页版）。

启用条件：环境变量 LP_PASSWORD，或设置里的访问密码且以无界面方式运行（LP_HEADLESS=1，Docker 镜像默认）。
桌面版本身只监听 127.0.0.1，设置里的密码不生效，免得桌面自己的提醒轮询被挡。
登录后发 HttpOnly 会话 cookie（HMAC 签名，含过期时间），改密码后旧 cookie 自动失效。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from backend.app.core.config import data_dir, read_settings_file

COOKIE = "lp_session"
MAX_AGE = 30 * 24 * 3600
EXEMPT = ("/api/health", "/api/auth/", "/api/wecom/callback")
ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        again = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), ITERATIONS)
        return hmac.compare_digest(again, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def headless() -> bool:
    return os.environ.get("LP_HEADLESS", "0") == "1"


def _stored() -> str:
    """返回 'env:<pw>' 或 settings 里的哈希，没设置返回空。"""
    env = os.environ.get("LP_PASSWORD", "")
    if env:
        return "env:" + env
    if headless():
        return str(read_settings_file().get("access_password") or "")
    return ""


def required() -> bool:
    return bool(_stored())


def check_password(password: str) -> bool:
    stored = _stored()
    if not stored:
        return True
    if stored.startswith("env:"):
        return hmac.compare_digest(password.encode("utf-8"), stored[4:].encode("utf-8"))
    return _check_hash(password, stored)


def _secret() -> bytes:
    path = data_dir() / ".session_secret"
    try:
        value = path.read_bytes()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def _sign(expires: int) -> str:
    fingerprint = hashlib.sha256(_stored().encode("utf-8")).hexdigest()[:16]  # 改密码 → 旧会话失效
    mac = hmac.new(_secret(), f"{expires}:{fingerprint}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{mac}"


def issue() -> str:
    return _sign(int(time.time()) + MAX_AGE)


def valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        expires = int(token.split(".", 1)[0])
    except ValueError:
        return False
    if expires < time.time():
        return False
    return hmac.compare_digest(token, _sign(expires))


def exempt(path: str) -> bool:
    if not path.startswith("/api"):
        return True  # 静态页面本身不挡，页面里会显示登录框
    return any(path == p.rstrip("/") or path.startswith(p) for p in EXEMPT)
