"""多套大模型网关（AI 源）：清单落一个 JSON，页面上增删改 + 一键切换。

为什么要单独一张清单，而不是把地址、模型、密钥拆成三个字段各改各的：
一套网关是**地址 + 模型名 + 密钥**三样一起才能用的一件事。分散着改会出现
「地址换成 NAS 那家、密钥还是另一家那把」这种拼盘——症状不是配置报错，
而是每次调用都 401，而人只会觉得「模型坏了」。按「源」管理之后，
一次切换换掉的是整三件套，`ai_gateway.ai_config()` 永远只回一套自洽的凭据。

三条底线：

1. **点一下就生效**：切换落盘后先丢掉这一层自己的缓存，再把进程内那份上一次分析的结论清掉。
   只清文件缓存的话，「切了源、面板上还是老网关给的那一份」会一直挂到十分钟 TTL 到点。
2. **活得过重启**：清单写在 `data_dir()/ai_sources.json`，配置每次冷读都先读它。
3. **密钥只进不出**：这个文件是密钥唯一的落脚点；接口只回 `key_present` 一个布尔，
   地址出接口前一律先过 `mask_url_secret()`（自建网关有一种把 token 写在路径里的形态），
   日志里只允许出现名字。

这一档**不发任何探测请求**：新增、编辑、切换都不敲网关，一次都不花钱。
代价要说清——地址写错了不会当场发现，要等下一次真分析才报上游失败。

「设置页里那三项」是清单第一条，也是**兜底那一条**：它就是 `settings.json` 里那三格
（环境变量 `AI_BASE_URL/AI_MODEL/AI_API_KEY` 折叠进来的也是同一处）。它改不了也删不掉，
要动它请回设置页上面那三格。`active_id` 空着就是用它。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from backend.app.core.config import (
    MASK,
    data_dir,
    masked_placeholder_in,
    mask_url_secret,
    normalize_ai_base_url,
    read_settings_file,
)
from backend.app.core.constants import AI_SOURCE_MAX, AI_SOURCE_NAME_MAX
from backend.app.core.logging import get_logger

logger = get_logger("ai_sources")

#: 内置那条的 id。前缀 `s-` 保证清单里新建的 id 永远撞不上它。
SETTINGS_SOURCE_ID = "settings"
SETTINGS_SOURCE_NAME = "设置页里那三项"

DOCUMENT_VERSION = 1
#: 整份重写用的字段名。页面编辑时「留空 = 不改」的就是这四列。
UPSERT_FIELDS = ("name", "base_url", "model", "api_key")

_ID_SHAPE = re.compile(r"^s-[0-9a-f]{8}$")


def sources_path() -> Path:
    return data_dir() / "ai_sources.json"


def _empty_document() -> dict:
    return {
        "version": DOCUMENT_VERSION,
        "active_id": SETTINGS_SOURCE_ID,
        "sources": [],
        "changed_by": "",
        "updated_at": "",
    }


_DOCUMENT_LOCK = threading.Lock()
_DOCUMENT_CACHE: dict | None = None
_DOCUMENT_PATH = ""
_DOCUMENT_SIGNATURE: tuple[int, int] | None = None
_DOCUMENT_READ_AT = 0.0
#: 同进程改清单会立刻失效，这里只给外部编辑器留一个上限：0.25 秒内不重复 stat。
DOCUMENT_TTL_SECONDS = 0.25
#: 最近一次读文件有没有把里面那份清单丢掉、为什么丢。只在进程内有效，
#: 靠接口里的 `note` 说给人听（不落盘、不回显密钥）。
_warning = ""


def warning() -> str:
    """坏在哪一层。空串表示一切正常。"""
    return _warning


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def read_document() -> dict:
    """读 `ai_sources.json`；文件缺失、坏了、形状不对都回空文档。

    **这里绝不能抛异常**：配置读取发生在应用启动最前面，一个手抖改坏的 json
    不该让整个后端起不来——回空文档就等于「当前用设置页那三项」，
    页面再点一次就能修好，而坏在哪一层由 `warning()` 在接口上明说。
    """
    global _warning, _DOCUMENT_CACHE, _DOCUMENT_PATH, _DOCUMENT_SIGNATURE, _DOCUMENT_READ_AT
    path = sources_path()
    text_path = str(path)
    now = time.monotonic()
    with _DOCUMENT_LOCK:
        if (
            _DOCUMENT_CACHE is not None
            and _DOCUMENT_PATH == text_path
            and now - _DOCUMENT_READ_AT < DOCUMENT_TTL_SECONDS
        ):
            return json.loads(json.dumps(_DOCUMENT_CACHE))
    signature = _signature(path)
    with _DOCUMENT_LOCK:
        if (
            _DOCUMENT_CACHE is not None
            and _DOCUMENT_PATH == text_path
            and _DOCUMENT_SIGNATURE == signature
        ):
            _DOCUMENT_READ_AT = now
            return json.loads(json.dumps(_DOCUMENT_CACHE))
    document = _empty_document()
    if signature is None:
        _warning = "" if not path.exists() else f"{path} 读不到（文件存在但 stat 失败）"
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _warning = f"{path} 不是能读的 JSON（{type(exc).__name__}），这一版先按「{SETTINGS_SOURCE_NAME}」跑"
            logger.warning("%s", _warning)
        else:
            if not isinstance(payload, dict):
                _warning = f"{path} 最外层不是对象，这一版先按「{SETTINGS_SOURCE_NAME}」跑"
            else:
                document = _sanitise(payload)
                _warning = document.pop("_note", "") or ""
    with _DOCUMENT_LOCK:
        _DOCUMENT_CACHE = document
        _DOCUMENT_PATH = text_path
        _DOCUMENT_SIGNATURE = signature
        _DOCUMENT_READ_AT = time.monotonic()
    return json.loads(json.dumps(document))


def _clean_source(raw: object) -> dict | None:
    """一条源能信多少收多少。形状不对就整条丢掉，绝不补出一个半套凭据。"""
    if not isinstance(raw, dict):
        return None
    source_id = str(raw.get("id") or "").strip()
    if not source_id or source_id == SETTINGS_SOURCE_ID or not _ID_SHAPE.match(source_id):
        return None
    try:
        base_url = normalize_ai_base_url(str(raw.get("base_url") or ""))
    except ValueError:
        # 已经存进去的地址坏了（多半是手改文件）：留着原样才能让人在页面上看出坏在哪，
        # 但它不能当凭据用——`resolve()` 那边会把它挡掉，这里只负责把它画出来。
        base_url = str(raw.get("base_url") or "").strip()
    return {
        "id": source_id,
        "name": str(raw.get("name") or "")[:AI_SOURCE_NAME_MAX],
        "base_url": base_url,
        "model": str(raw.get("model") or "").strip(),
        "api_key": str(raw.get("api_key") or "").strip(),
    }


def _sanitise(payload: dict) -> dict:
    sources: list[dict] = []
    dropped = 0
    raw_sources = payload.get("sources")
    if isinstance(raw_sources, list):
        for item in raw_sources:
            clean = _clean_source(item)
            if clean is None:
                dropped += 1
                continue
            sources.append(clean)
    note = ""
    if dropped:
        note = f"清单里有 {dropped} 条形状不对，这一版没有把它们摊进配置"
    active_id = str(payload.get("active_id") or "").strip() or SETTINGS_SOURCE_ID
    if active_id != SETTINGS_SOURCE_ID and not any(item["id"] == active_id for item in sources):
        note = note or f"清单里没有 id={active_id} 这条，当前用的是「{SETTINGS_SOURCE_NAME}」"
        active_id = SETTINGS_SOURCE_ID
    return {
        "version": DOCUMENT_VERSION,
        "active_id": active_id,
        "sources": sources,
        "changed_by": str(payload.get("changed_by") or "")[:200],
        "updated_at": str(payload.get("updated_at") or ""),
        "_note": note,
    }


def write_document(document: dict, *, changed_by: str) -> None:
    """整份重写：临时文件 + rename，读侧永远不会看到写了一半的 JSON。

    `api_key` 是这份文件里唯一的敏感列，它不进日志、不进接口、不进错误文案。
    """
    global _DOCUMENT_CACHE, _DOCUMENT_PATH, _DOCUMENT_SIGNATURE
    path = sources_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": DOCUMENT_VERSION,
        "active_id": str(document.get("active_id") or SETTINGS_SOURCE_ID),
        "sources": [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "base_url": str(item.get("base_url") or ""),
                "model": str(item.get("model") or ""),
                "api_key": str(item.get("api_key") or ""),
            }
            for item in (document.get("sources") or [])
            if isinstance(item, dict)
        ],
        "changed_by": str(changed_by or "")[:200],
        "updated_at": _now_iso(),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    tmp.replace(path)
    with _DOCUMENT_LOCK:
        _DOCUMENT_CACHE = None
        _DOCUMENT_PATH = ""
        _DOCUMENT_SIGNATURE = None


def _now_iso() -> str:
    from datetime import datetime

    from backend.app.core.clock import SHANGHAI

    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def new_source_id() -> str:
    """新建一条源用的 id：`s-` + 8 位十六进制。

    为什么不用自增数字、也不用名字当 id：这份文件是**整份重写**的，
    改名会把 `active_id` 指空，而数字在删一条之后会重复。
    """
    return f"s-{uuid.uuid4().hex[:8]}"


def settings_source() -> dict:
    """内置第一条：设置页那三格（环境变量折叠进来的也是这一处）。"""
    raw = read_settings_file()
    base_url = str(raw.get("ai_base_url") or "").strip()
    try:
        base_url = normalize_ai_base_url(base_url)
    except ValueError:
        # 坏地址照样要画出来（不然页面上这一条是空的，看不出坏在哪），但它不能当凭据生效
        pass
    return {
        "id": SETTINGS_SOURCE_ID,
        "name": SETTINGS_SOURCE_NAME,
        "base_url": base_url,
        "model": str(raw.get("ai_model") or "").strip(),
        "api_key": str(raw.get("ai_api_key") or "").strip(),
    }


def usable(source: dict) -> bool:
    """一套凭据齐不齐：地址合法、有模型名、有密钥。缺一件就是一次 401 或一次空请求。"""
    try:
        base_url = normalize_ai_base_url(source.get("base_url") or "")
    except ValueError:
        return False
    return bool(base_url and str(source.get("model") or "").strip() and str(source.get("api_key") or "").strip())


def list_sources() -> list[dict]:
    """内存里看到的清单：内置那条永远排第一，后面是文件里的那些。

    返回值**带着密钥**，只给 `resolve()` / `known_keys()` 这类内部用法；
    出接口必须走 `describe()`，那一层没有密钥字段。
    """
    return [settings_source(), *read_document()["sources"]]


def find(source_id: str) -> dict | None:
    for item in list_sources():
        if item["id"] == source_id:
            return item
    return None


def active_id() -> str:
    return read_document()["active_id"]


def active_source() -> dict:
    """当前生效的那一条。指向的那条没了或坏了，就退回内置那条并把原因写进 `warning` 之外。

    退回不是静默换网关：`describe()` 会把 `active_id` 归正成真正在用的那条，
    页面上「当前」那一格画的就是它，所以「以为在用 A、实际在敲 B」这件事画不出来之前
    先在接口上说不通。
    """
    document = read_document()
    wanted = str(document.get("active_id") or SETTINGS_SOURCE_ID)
    builtin = settings_source()
    if wanted == SETTINGS_SOURCE_ID:
        return builtin
    for item in document["sources"]:
        if item["id"] == wanted:
            return item
    return builtin


def resolve_config() -> dict:
    """三条付费链路唯一的那一只手：回一套自洽的 `base_url/model/api_key`。

    `ai_gateway.ai_config()` 读它，业务代码一个字都不用改。齐不齐由调用方判
    （`ai_ready()` 那边缺任何一项就是「一分钱都不会花」），这里不替它下结论。
    """
    source = active_source()
    return {
        "base_url": str(source.get("base_url") or "").strip().rstrip("/"),
        "model": str(source.get("model") or "").strip(),
        "api_key": str(source.get("api_key") or "").strip(),
        "source_id": str(source.get("id") or SETTINGS_SOURCE_ID),
        "source_name": str(source.get("name") or SETTINGS_SOURCE_NAME),
    }


def known_keys() -> tuple[str, ...]:
    """清单里所有密钥。给 `scrub_secrets` 当原文替换的名单——
    上游回显请求 URL 时，只有按原文顶掉才拦得住那种不带数字的钥匙。
    """
    return tuple(str(item.get("api_key") or "") for item in list_sources() if item.get("api_key"))


def describe() -> dict:
    """把清单摊成接口形态。地址在这里打码，密钥在这里消失。

    `active_id` 从 `active_source()` 读而不是从文件里那份读：文件里指空的那一种，
    页面上「当前」要画的是**真的在用**的那一条。
    """
    document = read_document()
    active = active_source()
    rows = [settings_source(), *document["sources"]]
    return {
        "sources": [
            {
                "id": item["id"],
                "name": item["name"] or "（没起名）",
                "base_url": mask_url_secret(item["base_url"]),
                "model": item["model"],
                "key_present": bool(item["api_key"]),
                "active": item["id"] == active["id"],
                "builtin": item["id"] == SETTINGS_SOURCE_ID,
                #: 三件套齐不齐。不齐的那条页面上标出来：切过去只会得到一次 401 或一次空请求。
                "complete": usable(item),
            }
            for item in rows
        ],
        "active_id": str(active["id"]),
        "active_name": str(active["name"]),
        "file_path": str(sources_path()),
        "max_sources": AI_SOURCE_MAX,
        #: 上限数的是**能被删改的那几条**，内置那行不占格子。计数由这里出，页面只读，
        #: 免得前端自己 `filter(row => !row.builtin)` 写第二把尺子、哪天口径漂了没人发现。
        "custom_count": len(document["sources"]),
        "name_max": AI_SOURCE_NAME_MAX,
        "note": warning(),
        "disclaimer": "切换网关不敲网关：这一页一次都不花钱，地址写错要等下一次真分析才看得出来。",
    }


# --- 写侧：校验与四种改动 -----------------------------------------------------


def _require_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label}不能留空")
    return text


def _require_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("网关地址不能留空")
    if masked_placeholder_in(text):
        raise ValueError(
            f"网关地址里带着打码占位符 {MASK}——那是接口回显给你认门用的形状，"
            f"把它改回真实地址再存"
        )
    try:
        return normalize_ai_base_url(text)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _guard_name(name: str, sources: list[dict], *, keep: str = "") -> None:
    if len(name) > AI_SOURCE_NAME_MAX:
        raise ValueError(f"名字最多 {AI_SOURCE_NAME_MAX} 个字，当前 {len(name)} 个字")
    for item in sources:
        if item["id"] != keep and str(item.get("name") or "") == name:
            raise ValueError(f"已经有名叫「{name}」的源了，换一个")


def _builtin_refusal(action: str, source_id: str) -> None:
    if source_id == SETTINGS_SOURCE_ID:
        raise ValueError(
            f"「{SETTINGS_SOURCE_NAME}」这一条{action}不了：它读的就是设置页上面那三格，"
            "要改请到那三格里改"
        )


def create_source(payload: dict) -> dict:
    """新增一条源。**不会**把它设成当前生效的那条——加源和换源是两件事。"""
    document = read_document()
    name = _require_text(payload.get("name"), "名字")
    base_url = _require_url(payload.get("base_url"))
    model = _require_text(payload.get("model"), "模型名")
    _guard_name(name, document["sources"])
    if len(document["sources"]) >= AI_SOURCE_MAX:
        raise ValueError(f"清单里已经有 {AI_SOURCE_MAX} 条，先删掉一条再加")
    source = {
        "id": new_source_id(),
        "name": name,
        "base_url": base_url,
        "model": model,
        "api_key": str(payload.get("api_key") or "").strip(),
    }
    document["sources"].append(source)
    _write(document, changed_by="settings 页新增 AI 源")
    logger.info("已新增 AI 源：%s（id=%s，密钥%s）", source["name"], source["id"], "已配" if source["api_key"] else "未配")
    return describe()


def update_source(source_id: str, payload: dict) -> dict:
    """编辑一条源；`payload` 里留空的列沿用原值。"""
    _builtin_refusal("修改", source_id)
    document = read_document()
    current = _require_source(document, source_id)
    changes = {}
    for field in UPSERT_FIELDS:
        text = str(payload.get(field) or "").strip()
        if text:
            changes[field] = text
    if not changes:
        raise ValueError("没有需要修改的字段")
    merged = {**current, **changes}
    if "name" in changes:
        _guard_name(str(merged["name"]), document["sources"], keep=source_id)
    if "base_url" in changes:
        merged["base_url"] = _require_url(merged["base_url"])
    document["sources"] = [merged if item["id"] == source_id else item for item in document["sources"]]
    _write(document, changed_by="settings 页修改 AI 源")
    logger.info("已修改 AI 源：%s（id=%s）", merged["name"], source_id)
    if document["active_id"] == source_id:
        #: 改的正是当前生效那一套：面板上那一份结论是改之前的网关给的
        invalidate_caches()
    return describe()


def delete_source(source_id: str) -> dict:
    """删一条源。正在用的那条先切走再删——留下一个没有出处的配置不如不让删。"""
    _builtin_refusal("删除", source_id)
    document = read_document()
    target = _require_source(document, source_id)
    if document["active_id"] == source_id:
        raise ValueError(f"「{target['name']}」正在被使用，先在页面上切到别的源，再回来删它")
    document["sources"] = [item for item in document["sources"] if item["id"] != source_id]
    _write(document, changed_by="settings 页删除 AI 源")
    logger.info("已删除 AI 源：%s（id=%s）", target["name"], source_id)
    return describe()


def set_active(source_id: str) -> dict:
    """一键切换：当场生效、活过重启，三条花钱链路（综合分析 / 固定榜排名 / 单股直评）一起跟着走。"""
    document = read_document()
    if source_id == SETTINGS_SOURCE_ID:
        target = settings_source()
    else:
        target = _require_source(document, source_id)
    if document["active_id"] == source_id:
        return describe()
    document["active_id"] = source_id
    _write(document, changed_by="settings 页一键切换 AI 源")
    invalidate_caches()
    logger.info(
        "AI 源已切换为：%s（id=%s，密钥%s）",
        target["name"],
        source_id,
        "已配" if target["api_key"] else "未配",
    )
    return describe()


def _require_source(document: dict, source_id: str) -> dict:
    for item in document["sources"]:
        if item["id"] == source_id:
            return item
    raise ValueError(f"清单里没有 id={source_id} 这条 AI 源，请刷新页面重来")


def _write(document: dict, *, changed_by: str) -> None:
    try:
        write_document(document, changed_by=changed_by)
    except OSError as exc:
        raise ValueError(f"这条 AI 源存不进去：{type(exc).__name__}") from exc


def invalidate_caches() -> None:
    """让下一次读配置重读文件，并把上一次分析留在内存里的那份结论清掉。

    清缓存这一步不是顺手做的：切了源而面板还挂着老网关那份结论，
    用户看到的「模型」和真的会敲的那家对不上——那比一次报错更难查。
    线程正在跑的那一趟追不回来（它已经拿着旧凭据发出去了），这里只保证**下一次**是新的。
    """
    global _DOCUMENT_CACHE, _DOCUMENT_PATH, _DOCUMENT_SIGNATURE
    with _DOCUMENT_LOCK:
        _DOCUMENT_CACHE = None
        _DOCUMENT_PATH = ""
        _DOCUMENT_SIGNATURE = None
    try:
        from backend.app.core import cache

        cache.cache_clear()
    except Exception:  # noqa: BLE001 - 清不掉缓存不该让切换失败
        logger.info("切源后清进程缓存没有做成")
    try:
        from backend.app.services.ai_analysis_service import board_runner, single_runner

        board_runner.reset()
        single_runner.reset()
    except Exception:  # noqa: BLE001 - 上面那两条链路是惰性的，导入失败也不能挡住落盘已经成功这件事
        logger.info("切源后清 AI 报告内存槽位没有做成")
