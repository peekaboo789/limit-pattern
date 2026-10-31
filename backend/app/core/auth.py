"""账号登录（v0.5.1：用户名 + 密码，记住我 = 长期 cookie）。

何时需要登录：
- 设置了环境变量 LP_PASSWORD（用户名 LP_USER，默认 admin）——任何运行方式；
- 或以无界面方式运行（LP_HEADLESS=1，Docker 默认）：没有账号时先显示「创建管理员账号」。
桌面版（本机 pywebview 窗口）不需要登录，和以前一样。

存储：数据目录 auth.json（用户名、PBKDF2-SHA256 加盐哈希、会话列表、epoch），签名密钥 .session_secret。
两者都在数据目录里，重启 / 在线更新 / 备份都会保留，所以登录状态一直有效。
cookie = sid.签名；签名覆盖 sid + 凭据指纹（用户名+密码哈希）+ epoch。改用户名/密码或「退出所有设备」后旧 cookie 全部失效。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from backend.app.core.config import data_dir, read_settings_file

COOKIE = "lp_session"
REMEMBER_AGE = 10 * 365 * 24 * 3600  # 「记住我」：实际上永久，直到改密码或退出
EXEMPT = ("/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/wecom/callback")
ITERATIONS = 240_000
MAX_FAILS, FAIL_WINDOW, LOCK_SECONDS = 5, 300, 300
TOUCH_EVERY = 600
_LOCK = threading.RLock()
_FAILS: dict[str, list[float]] = {}
_LOCKED: dict[str, float] = {}


# ---------- 哈希 ----------
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_hash(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if len(parts) == 3:  # v0.5.0 格式：pbkdf2$salt$digest（200000 轮）
            _, salt, digest = parts
            rounds = 200_000
        else:
            _, rounds_s, salt, digest = parts
            rounds = int(rounds_s)
        again = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), rounds)
        return hmac.compare_digest(again, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# ---------- 账号存储 ----------
def _path():
    return data_dir() / "auth.json"


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def headless() -> bool:
    return os.environ.get("LP_HEADLESS", "0") == "1"


def env_account() -> tuple[str, str] | None:
    pw = os.environ.get("LP_PASSWORD", "")
    return ((os.environ.get("LP_USER", "").strip() or "admin"), pw) if pw else None


def migrate() -> bool:
    """v0.5.0 设置里的 access_password（只有密码）→ auth.json 的 admin 账号，原密码继续能用。"""
    with _LOCK:
        data = _load()
        if data.get("hash"):
            return False
        old = str(read_settings_file().get("access_password") or "")
        if not old.startswith("pbkdf2$"):
            return False
        data.update(username="admin", hash=old, sessions=[], epoch=0, migrated_from="0.5.0")
        _save(data)
    try:
        from backend.app.core.config import _SETTINGS_LOCK, _load_settings_payload, settings_path, write_settings_file

        with _SETTINGS_LOCK:
            payload = _load_settings_payload(settings_path())
            if isinstance(payload, dict):
                payload["access_password"] = ""
                write_settings_file(payload)
    except Exception:  # noqa: BLE001
        pass
    return True


def account() -> dict | None:
    """{username, hash|None, from_env}；没有账号返回 None。"""
    env = env_account()
    if env:
        return {"username": env[0], "hash": None, "from_env": True, "env_password": env[1]}
    migrate()
    data = _load()
    if data.get("hash") and data.get("username"):
        return {"username": data["username"], "hash": data["hash"], "from_env": False}
    return None


def required() -> bool:
    return env_account() is not None or headless()


def setup_needed() -> bool:
    return required() and account() is None


def check_credentials(username: str, password: str) -> bool:
    acc = account()
    if acc is None or not hmac.compare_digest(username.strip().lower().encode(), acc["username"].lower().encode()):
        verify_hash(password, hash_password("x"))  # 用户名不对也花同样的时间
        return False
    if acc["from_env"]:
        return hmac.compare_digest(password.encode("utf-8"), acc["env_password"].encode("utf-8"))
    return verify_hash(password, acc["hash"])


def create_account(username: str, password: str) -> None:
    _validate(username, password)
    with _LOCK:
        if account() is not None:
            raise ValueError("已经有账号了")
        _save({"username": username.strip(), "hash": hash_password(password), "sessions": [], "epoch": 0})


def change(current: str, username: str | None, new_password: str | None) -> None:
    acc = account()
    if acc is None:
        raise ValueError("还没有账号")
    if acc["from_env"]:
        raise ValueError("账号来自环境变量 LP_USER / LP_PASSWORD，请改环境变量后重启")
    if not check_credentials(acc["username"], current):
        raise PermissionError("当前密码不对")
    username = (username or acc["username"]).strip()
    _validate(username, new_password or current)
    with _LOCK:
        data = _load()
        data["username"] = username
        if new_password:
            data["hash"] = hash_password(new_password)
        data["sessions"] = []  # 凭据变了，所有设备重新登录
        _save(data)


def _validate(username: str, password: str) -> None:
    if not (1 <= len(username.strip()) <= 32) or any(c in username for c in " :\t\n"):
        raise ValueError("用户名 1–32 位，不能有空格或冒号")
    if len(password) < 6:
        raise ValueError("密码至少 6 位")


# ---------- 签名与会话 ----------
def _secret() -> bytes:
    path = data_dir() / ".session_secret"
    with _LOCK:
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


def _fingerprint() -> str:
    acc = account() or {}
    material = f"{acc.get('username', '')}:{acc.get('hash') or hashlib.sha256(acc.get('env_password', '').encode()).hexdigest()}:{_load().get('epoch', 0)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _sign(sid: str) -> str:
    return hmac.new(_secret(), f"{sid}:{_fingerprint()}".encode(), hashlib.sha256).hexdigest()


def issue(user_agent: str = "", ip: str = "", remember: bool = True) -> str:
    sid = secrets.token_urlsafe(18)
    now = int(time.time())
    with _LOCK:
        data = _load()
        sessions = [s for s in data.get("sessions", []) if isinstance(s, dict)][-49:]
        sessions.append({"id": sid, "ua": user_agent[:160], "ip": ip, "created": now, "last_seen": now, "remember": remember})
        data["sessions"] = sessions
        _save(data)
    return f"{sid}.{_sign(sid)}"


def _sid(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    sid, mac = token.split(".", 1)
    return sid if hmac.compare_digest(mac, _sign(sid)) else None


def valid(token: str | None) -> bool:
    sid = _sid(token)
    if sid is None:
        return False
    data = _load()
    if env_account() is None and not data.get("hash"):
        return False
    hit = next((s for s in data.get("sessions", []) if s.get("id") == sid), None)
    if hit is None and env_account() is None:
        return False
    if hit is None:  # 环境变量账号：auth.json 可能没写会话（只读挂载等），签名对就行
        return True
    if time.time() - hit.get("last_seen", 0) > TOUCH_EVERY:
        with _LOCK:
            fresh = _load()
            for s in fresh.get("sessions", []):
                if s.get("id") == sid:
                    s["last_seen"] = int(time.time())
            _save(fresh)
    return True


def logout(token: str | None) -> None:
    sid = _sid(token)
    if not sid:
        return
    with _LOCK:
        data = _load()
        data["sessions"] = [s for s in data.get("sessions", []) if s.get("id") != sid]
        _save(data)


def logout_all() -> None:
    with _LOCK:
        data = _load()
        data["sessions"] = []
        data["epoch"] = int(data.get("epoch", 0)) + 1
        _save(data)


def sessions(token: str | None) -> list[dict]:
    me = _sid(token)
    return [{"id": s["id"][:6], "ua": s.get("ua", ""), "ip": s.get("ip", ""), "created": s.get("created"), "last_seen": s.get("last_seen"),
             "current": s.get("id") == me} for s in reversed(_load().get("sessions", []))]


# ---------- 失败限流 ----------
def locked_for(ip: str) -> int:
    until = _LOCKED.get(ip, 0)
    return max(0, int(until - time.time()))


def record_failure(ip: str) -> int:
    now = time.time()
    with _LOCK:
        hits = [t for t in _FAILS.get(ip, []) if now - t < FAIL_WINDOW] + [now]
        _FAILS[ip] = hits
        if len(hits) >= MAX_FAILS:
            _LOCKED[ip] = now + LOCK_SECONDS
            _FAILS[ip] = []
    return MAX_FAILS - len(_FAILS.get(ip, []))


def record_success(ip: str) -> None:
    _FAILS.pop(ip, None)


def reset_limits() -> None:
    _FAILS.clear()
    _LOCKED.clear()


def exempt(path: str) -> bool:
    if not path.startswith("/api"):
        return True  # 静态页面本身不挡，页面里显示登录框
    return any(path == p or path.startswith(p + "/") for p in EXEMPT)
