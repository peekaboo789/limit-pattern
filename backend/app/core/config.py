"""运行配置。数据目录只认 LP_DATA_DIR，否则用用户目录。"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.core.constants import (
    COMPARE_WINDOW_MIN_TRADING_DAYS,
    COMPARE_WINDOW_TRADING_DAYS,
    DETAIL_RETENTION_TRADING_DAYS,
    RETENTION_MAX_TRADING_DAYS,
    RETENTION_MIN_TRADING_DAYS,
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


_DATA_DIR_LOCK = threading.Lock()
_DATA_DIR_CACHE: tuple[str, str, Path] | None = None


def data_dir() -> Path:
    """行情库、设置和每日总结都放在这里。

    一轮盘中扫描要给四千多只股票判板块，每只都要问一次设置，设置又落在这个目录里。
    每次问都重建 Path、stat 一遍再 mkdir 的话，判定本身就把两秒的节奏占满了，
    所以这里按环境变量缓存路径。目录被外部删掉时下面那次 `is_dir()` 会把它补回来。
    """
    global _DATA_DIR_CACHE
    raw = os.environ.get("LP_DATA_DIR", "").strip()
    env_base = os.environ.get("LOCALAPPDATA", "") if os.name == "nt" else ""
    with _DATA_DIR_LOCK:
        cached = _DATA_DIR_CACHE
    if cached is not None and cached[0] == raw and cached[1] == env_base:
        path = cached[2]
        if path.is_dir():
            return path
    if raw:
        path = Path(raw).expanduser()
    elif os.name == "nt":
        base = env_base or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "LimitPattern"
    else:
        path = Path.home() / ".limit_pattern"
    path.mkdir(parents=True, exist_ok=True)
    with _DATA_DIR_LOCK:
        _DATA_DIR_CACHE = (raw, env_base, path)
    return path


def summaries_dir() -> Path:
    path = data_dir() / "summaries"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def static_dir() -> Path | None:
    raw = os.environ.get("LP_STATIC_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_dir() else None
    bundled = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if bundled.is_dir():
        return bundled
    return None


class Settings(BaseSettings):
    """给还在用环境变量的旧模块留的壳。业务设置以 settings.json 为准。"""

    model_config = SettingsConfigDict(extra="ignore")

    redis_url: str = ""
    cache_enabled: bool = False
    supported_exchanges: str = "SSE,SZSE"
    data_provider: str = "eastmoney"

    def resolved_database_url(self) -> str:
        override = os.environ.get("DATABASE_URL", "").strip()
        if override:
            return override
        return "sqlite:///" + (data_dir() / "lp.db").as_posix()

    def exchange_set(self) -> set[str]:
        return {part.strip().upper() for part in self.supported_exchanges.split(",") if part.strip()}

    def redis_explicit(self) -> bool:
        return bool(os.environ.get("REDIS_URL", "").strip() or self.redis_url.strip())


_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE: tuple[tuple[str, ...], "Settings"] | None = None
_SETTINGS_ENV = ("REDIS_URL", "CACHE_ENABLED", "SUPPORTED_EXCHANGES", "DATA_PROVIDER")


def get_settings() -> Settings:
    """`Settings` 只读那四个环境变量，值没变就用同一份。

    判一只股票在不在纳入范围要问它一次，一轮全市场就是四千多次；
    pydantic-settings 每次构造都要把整个环境扫一遍。
    """
    global _SETTINGS_CACHE
    key = tuple(os.environ.get(name, "") for name in _SETTINGS_ENV)
    with _SETTINGS_LOCK:
        cached = _SETTINGS_CACHE
    if cached is not None and cached[0] == key:
        return cached[1]
    settings = Settings()
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE = (key, settings)
    return settings


def scrub_secrets(text: str, keys: tuple[str, ...] = ()) -> str:
    """把任意文案里可能当凭据的东西抹掉：先按已知密钥原文替换，再扫「像密钥」的串。

    为什么要按原文替换：密钥本身没有固定形状，光靠正则挡不住。为什么还要再叠一层正则：
    上游的报错页面经常把我们请求的完整 URL 原样回显回来，那里面就可能带着写在路径里的密钥
    （自建网关有一种把 token 塞在 path 里的写法）。
    `keys` 给的是「这一趟知道的别把密钥」——网关清单里那 20 条各有自己的密钥，
    报错文案来自哪一条只有调用方清楚，所以由调用方把它们一并交进来。
    """
    out = str(text or "")
    known = list(keys)
    env_key = os.environ.get("AI_API_KEY", "")
    if env_key:
        known.append(env_key)
    stored = read_settings_file().get("ai_api_key") or ""
    if stored:
        known.append(str(stored))
    for secret in known:
        text_key = str(secret or "")
        if len(text_key) >= 6:
            out = out.replace(text_key, MASK)
    return _SECRET_TOKEN.sub(_mask_token, out)


#: 打码占位符。接口回显的地址里出现它，就说明这一串是从接口回来的，不是人手写的原值。
MASK = "***"

#: 「这一段像密钥」的判据：`sk-…` / `rc-…` / `apikey_…` 这种「短前缀 + 分隔符 + 长随机串」。
#: 分隔符后面至少要 16 个字符，免得把 `chat-completions` 这类正常路径段也打码了
#: （`completions` 只有 11 个字符，落不进这条判据）。
#: 不锚定整行：同一串既用来逐段比对 URL 路径段（`fullmatch`），也用来扫自由文案里的散落凭据。
_SECRET_TOKEN = re.compile(r"(?P<head>[A-Za-z]{1,8})(?P<sep>[-_])(?P<tail>[A-Za-z0-9_-]{16,})")

#: query 里那些一眼就是凭据的参数名（`?key=…` 是不少自建网关的传法）。
_SECRET_QUERY_KEYS = re.compile(r"(key|token|secret|password|passwd|apikey)", re.I)


def _mask_token(match: re.Match[str]) -> str:
    """只留前缀（`sk-` / `rc-`）认得出是哪家，后面全顶掉。

    为什么还要再看尾串里有没有数字：上游 429 的文案里那种
    `"code":"process_rate_limit_exceeded"` 会被同一条判据命中，整段打成 `process_***`
    等于把「病因」糊掉，而这一层的全部意义是**留下病因、只丢凭据**。
    密钥那类随机串基本都带数字，snake_case 的英文标识符不带，所以「尾串含数字」能把误伤挡住。
    代价要说清：一种罕见的密钥形状（16 位以上、纯字母）不再被**形状层**拦下——
    形状层本来就是兜底，配置里真正生效的那把仍由 `scrub_secrets` 的原文替换先顶掉。
    """
    tail = match.group("tail")
    if not any(char.isdigit() for char in tail):
        return match.group(0)
    return f"{match.group('head')}{match.group('sep')}{MASK}"


def mask_url_secret(url: str) -> str:
    """把网关地址里可能当凭据用的部分打码，只留「哪家网关」这一层信息。

    接口回显地址、日志、失败报告都先过这一道：有的自建网关把密钥直接写在路径或 query 里，
    那种地址原样出接口就等于把密钥发给了任何读得到这个接口的人。
    打码三处形态：`scheme://user:pass@host` 的口令、路径段里像密钥的那一串、
    query 里名字带 key/token/secret/password 的参数值。地址本身没有凭据时原样返回。
    """
    text = str(url or "")
    if not text:
        return text
    scheme, sep, rest = text.partition("://")
    if not sep:
        # 少了 `scheme://`（校验本来就会挡掉）也别原样吐出去，按整串处理
        scheme, rest = "", text
    credential, at, authority_and_path = rest.partition("@")
    if at:
        user, colon, _password = credential.partition(":")
        credential = f"{user}{colon}{MASK}" if colon else MASK
        rest = f"{credential}@{authority_and_path}"
    authority, slash, remainder = rest.partition("/")
    path, question, query = f"{slash}{remainder}".partition("?")
    segments = [
        _mask_token(match) if (match := _SECRET_TOKEN.fullmatch(segment)) else segment
        for segment in path.split("/")
    ]
    # `slash` 在时第一段是空串，拼出来自带前导 `/`，所以返回处不能再补斜杠
    rebuilt = "/".join(segments) if slash else ""
    if question:
        pairs = []
        for pair in query.split("&"):
            name, equals, value = pair.partition("=")
            pairs.append(
                f"{name}{equals}{MASK}" if equals and _SECRET_QUERY_KEYS.search(name) else pair
            )
        rebuilt = f"{rebuilt}?{'&'.join(pairs)}"
    return f"{scheme}{sep}{authority}{rebuilt}"


def masked_placeholder_in(value: object) -> bool:
    """这串里有没有打码占位符。有，它就只可能是接口回显的那一份，不可能是人手写的原值。

    为什么单列一个函数：页面表单里预填的就是打码后的地址，用户直接点保存就是把 `***`
    存回配置里，症状不是报错而是「每次调用都失败」。校验与保存两处都要认这个形状，
    写在两处将来会漂，所以收在这里。
    """
    return MASK in str(value or "")


def normalize_ai_base_url(value: str) -> str:
    """去尾斜杠 + 挡掉非 http(s) 的地址。设置页那一格与网关清单共用同一条规则。

    拼接处是 `f"{base}/chat/completions"`，留着尾斜杠会变成 `//v1//chat/completions`，
    那种路径有些网关直接 404；而 `openai.com` 这种少了协议的写法要在配置层就说清，
    不留到一次十分钟超时之后才发现。
    """
    cleaned = str(value or "").strip().rstrip("/")
    if not cleaned:
        return cleaned
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError(f"网关地址要带协议，形如 https://某个主机/v1，收到的是 {value!r}")
    return cleaned


def settings_path() -> Path:
    return data_dir() / "settings.json"


def default_settings() -> dict:
    return {
        "retention_trading_days": DETAIL_RETENTION_TRADING_DAYS,
        "compare_window_trading_days": COMPARE_WINDOW_TRADING_DAYS,
        "ai_base_url": os.environ.get("AI_BASE_URL", "").strip(),
        "ai_model": os.environ.get("AI_MODEL", "").strip(),
        "ai_api_key": os.environ.get("AI_API_KEY", "").strip(),
        "data_source": os.environ.get("DATA_PROVIDER", "auto").strip() or "auto",
        "active_source": "",
        "excluded_boards": ["star"],
        "last_sync_at": None,
        "last_cleanup_at": None,
        #: 质量闸门与出数来源账。`read_settings_file()` 只回这里列出的键，
        #: 所以 `stamp()` 想留的键必须先在这里登记，否则盖下去就读不回来。
        "last_quality_at": None,
        "last_quality_errors": None,
        "last_quality_warnings": None,
        "last_sync_sources": None,
    }


_SETTINGS_FILE_LOCK = threading.Lock()
_SETTINGS_FILE_CACHE: dict | None = None
_SETTINGS_FILE_PATH = ""
_SETTINGS_FILE_SIGNATURE: tuple[int, int] | None = None
_SETTINGS_FILE_READ_AT = 0.0
#: 同进程改设置会立刻失效，这里只是给外部编辑器留一个上限：0.25 秒内不重复 stat。
SETTINGS_FILE_TTL_SECONDS = 0.25


def _settings_file_signature(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def read_settings_file() -> dict:
    """读 settings.json，按「哪份文件 + mtime + 大小」缓存，返回浅拷贝。

    一轮盘中扫描要给四千多只股票判板块归属，每只都要问一次排除哪些板块；
    每次都读盘解析的话，光这件事就把两秒的节奏占满。
    缓存必须连着文件路径一起比：换数据目录（测试用临时目录、同时跑两份程序）
    要立刻读到那一份自己的设置，不能沿用上一份。
    调用方（`update_settings`、`stamp`）拿到的是拷贝，改它不会污染缓存。
    """
    global _SETTINGS_FILE_CACHE, _SETTINGS_FILE_PATH, _SETTINGS_FILE_SIGNATURE, _SETTINGS_FILE_READ_AT
    path = settings_path()
    text_path = str(path)
    now = time.monotonic()
    with _SETTINGS_FILE_LOCK:
        if (
            _SETTINGS_FILE_CACHE is not None
            and _SETTINGS_FILE_PATH == text_path
            and now - _SETTINGS_FILE_READ_AT < SETTINGS_FILE_TTL_SECONDS
        ):
            return dict(_SETTINGS_FILE_CACHE)
    signature = _settings_file_signature(path)
    with _SETTINGS_FILE_LOCK:
        if (
            _SETTINGS_FILE_CACHE is not None
            and _SETTINGS_FILE_PATH == text_path
            and _SETTINGS_FILE_SIGNATURE == signature
        ):
            _SETTINGS_FILE_READ_AT = now
            return dict(_SETTINGS_FILE_CACHE)
    merged = default_settings()
    if signature is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in merged:
                if key in payload and payload[key] is not None:
                    merged[key] = payload[key]
    with _SETTINGS_FILE_LOCK:
        _SETTINGS_FILE_CACHE = merged
        _SETTINGS_FILE_PATH = text_path
        _SETTINGS_FILE_SIGNATURE = signature
        _SETTINGS_FILE_READ_AT = time.monotonic()
    return dict(merged)


def write_settings_file(payload: dict) -> None:
    global _SETTINGS_FILE_CACHE, _SETTINGS_FILE_PATH, _SETTINGS_FILE_SIGNATURE
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    tmp.replace(path)
    with _SETTINGS_FILE_LOCK:
        _SETTINGS_FILE_CACHE = None
        _SETTINGS_FILE_PATH = ""
        _SETTINGS_FILE_SIGNATURE = None


def excluded_boards() -> set[str]:
    """默认剔除科创板。环境变量 EXCLUDED_BOARDS 优先，空字符串表示一块都不剔。"""
    if "EXCLUDED_BOARDS" in os.environ:
        return {part.strip() for part in os.environ["EXCLUDED_BOARDS"].split(",") if part.strip()}
    raw = read_settings_file().get("excluded_boards")
    if isinstance(raw, str):
        return {part.strip() for part in raw.split(",") if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {"star"}


def public_settings() -> dict:
    raw = read_settings_file()
    key = str(raw.get("ai_api_key") or "")
    url = str(raw.get("ai_base_url") or "")
    model = str(raw.get("ai_model") or "")
    from backend.app.core.ai_sources import describe as describe_ai_sources

    #: 当前真的会敲哪一家，由网关清单说了算（清单第一条就是上面这三格）。
    #: 这里回两件事，各自说清：`ai_base_url/ai_model/ai_api_key_set` 是**表单要编辑的那三格**，
    #: `ai_active_*` 是**下一次请求实际用的那一套**。两者不一样时页面上必须都画出来，
    #: 不然「我明明改了地址，为什么还在打老网关」这件事没有出处。
    ai = describe_ai_sources()
    active = next((item for item in ai["sources"] if item["active"]), None) or {}
    return {
        "retention_trading_days": retention_days(),
        "compare_window_trading_days": compare_window_days(),
        "compare_window_min": COMPARE_WINDOW_MIN_TRADING_DAYS,
        "retention_min": RETENTION_MIN_TRADING_DAYS,
        "retention_max": RETENTION_MAX_TRADING_DAYS,
        "ai_enabled": bool(active.get("complete")),
        "ai_base_url": url,
        "ai_model": model,
        "ai_api_key_set": bool(key),
        "ai_active_id": str(active.get("id") or ""),
        "ai_active_name": str(active.get("name") or ""),
        "ai_active_base_url": str(active.get("base_url") or ""),
        "ai_active_model": str(active.get("model") or ""),
        "ai_active_key_present": bool(active.get("key_present")),
        "ai_source_count": len(ai["sources"]),
        "data_source": str(raw.get("data_source") or "auto"),
        "data_source_options": ["auto", "eastmoney", "tencent", "sina"],
        "active_source": str(raw.get("active_source") or ""),
        "excluded_boards": sorted(excluded_boards()),
        "included_boards": _included_board_names(),
        "market_scope": _market_scope_text(),
        "last_sync_at": raw.get("last_sync_at"),
        "last_cleanup_at": raw.get("last_cleanup_at"),
    }


def _included_board_names() -> list[str]:
    from backend.app.providers.markets import included_board_names

    return included_board_names()


def _market_scope_text() -> str:
    from backend.app.providers.markets import market_scope_text

    return market_scope_text()


def _whole_days(value: object, label: str) -> int:
    """设置里的天数只收整数。`3.7`、`true`、`"三"` 都直接拒，不悄悄取整。"""
    if isinstance(value, bool):
        raise ValueError(f"{label}要填整数天数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{label}要填整数天数，收到的是 {value!r}")


def update_settings(changes: dict) -> dict:
    current = read_settings_file()
    if "retention_trading_days" in changes and changes["retention_trading_days"] is not None:
        days = _whole_days(changes["retention_trading_days"], "保留天数")
        if days < RETENTION_MIN_TRADING_DAYS or days > RETENTION_MAX_TRADING_DAYS:
            raise ValueError(
                f"保留天数要在 {RETENTION_MIN_TRADING_DAYS} 到 {RETENTION_MAX_TRADING_DAYS} 之间"
            )
        current["retention_trading_days"] = days
    if "compare_window_trading_days" in changes and changes["compare_window_trading_days"] is not None:
        current["compare_window_trading_days"] = _whole_days(
            changes["compare_window_trading_days"], "对比窗口"
        )
    # 两个数一起核：样本池只能落在库里留着的那几天里。窗口比保留天数还长，
    # 池子最老的那几天早被清掉了，页面上写着「5 日窗口」实际只比了 3 天。
    # 设置文件里存的可能是老版本允许、如今已低于下限的值：按读取口径收回边界再核，
    # 不然页面显示 6、校验却拿文件里的 5 去卡对比窗口。
    retention = max(
        RETENTION_MIN_TRADING_DAYS,
        min(
            RETENTION_MAX_TRADING_DAYS,
            _whole_days(current.get("retention_trading_days", DETAIL_RETENTION_TRADING_DAYS), "保留天数"),
        ),
    )
    window = _whole_days(current.get("compare_window_trading_days", COMPARE_WINDOW_TRADING_DAYS), "对比窗口")
    if window < COMPARE_WINDOW_MIN_TRADING_DAYS or window > retention:
        raise ValueError(
            f"对比窗口要在 {COMPARE_WINDOW_MIN_TRADING_DAYS} 到保留天数 {retention} 之间，收到的是 {window}"
        )
    if "ai_base_url" in changes and changes["ai_base_url"] is not None:
        text = str(changes["ai_base_url"]).strip()
        if masked_placeholder_in(text):
            raise ValueError(
                f"网关地址里带着打码占位符 {MASK}——那是接口回显给你认门用的形状，改回真实地址再存"
            )
        try:
            current["ai_base_url"] = normalize_ai_base_url(text)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    if "ai_model" in changes and changes["ai_model"] is not None:
        current["ai_model"] = str(changes["ai_model"]).strip()
    if changes.get("ai_api_key"):
        key = str(changes["ai_api_key"]).strip()
        if masked_placeholder_in(key):
            raise ValueError(f"密钥那一格带着打码占位符 {MASK}：接口不会把密钥回给你，要改就填完整的那一把")
        current["ai_api_key"] = key
    if "data_source" in changes and changes["data_source"] is not None:
        from backend.app.providers.router import normalize_source

        current["data_source"] = normalize_source(str(changes["data_source"]))
    write_settings_file(current)
    return public_settings()


def stamp(field: str, value: object) -> None:
    """往 settings.json 盖一个键。值原样写：字符串、数字、dict 都行。

    一轮同步结束要留的那份「出数来源账」是 dict，为了一个键先把整个 dict 转成字符串，
    读的那头还得再 parse 一次，两处都得记着这个约定。这里直接存结构。
    """
    current = read_settings_file()
    current[field] = value
    write_settings_file(current)


def retention_days() -> int:
    try:
        days = int(read_settings_file()["retention_trading_days"])
    except (TypeError, ValueError, KeyError):
        return DETAIL_RETENTION_TRADING_DAYS
    return max(RETENTION_MIN_TRADING_DAYS, min(RETENTION_MAX_TRADING_DAYS, days))


def compare_window_days() -> int:
    """对比窗口（样本池往回看几个交易日，含基准日）。全工程只从这里取这个数。

    设置文件被手改坏、或者窗口比保留天数还长时，按边界收回来——
    读的这一头不报错，拒绝是 `update_settings` 在写入时做的事。
    """
    try:
        days = int(read_settings_file()["compare_window_trading_days"])
    except (TypeError, ValueError, KeyError):
        days = COMPARE_WINDOW_TRADING_DAYS
    return max(COMPARE_WINDOW_MIN_TRADING_DAYS, min(retention_days(), days))
