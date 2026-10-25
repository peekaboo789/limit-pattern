"""一句口径只写一处、被废掉的开关不许复活。

这一档管的是「脏行」：同一句话在后端抄三遍、前端再抄两遍时，改口径就要挨个文件翻，
漏掉一处就成了两套说法；已经删掉的开关（进程缓存、Redis）只要还有一行代码读环境变量，
下次就有人照着那行把它接回去。两个判据都是扫源码，不看运行结果——
它们要挡的是「有人又写了一遍」，不是「某个数字算错了」。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTENCE = "相似度不是涨停概率"

#: 前端那句顶栏声明的唯一来源；别的文件只许 import 它。
FRONTEND_HOME = "utils/disclaimer.ts"


def _sources(directory: Path) -> list[Path]:
    return [
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".ts", ".vue"}
        and "__pycache__" not in path.parts
        and path.name != "conftest.py"
    ]


def _files_saying(directory: Path, sentence: str) -> list[Path]:
    return [path for path in _sources(directory) if sentence in path.read_text(encoding="utf-8")]


def test_the_similarity_sentence_is_written_once_in_the_backend():
    """后端只有 `core/constants.py` 写这句话，其余模块一律 import 那个常量。

    盘中榜、相似度页、匹配明细、三日形态、导出表头、次日预测池都回传它。
    以前各留一份字面量，把其中一处改成「相似度不等于涨停概率」没有任何测试会红。
    """
    hits = _files_saying(ROOT / "backend", SENTENCE)
    assert [path.name for path in hits] == ["constants.py"], f"这句话又被抄了一遍：{hits}"
    constants = (ROOT / "backend" / "app" / "core" / "constants.py").read_text(encoding="utf-8")
    assert f'SIMILARITY_DISCLAIMER = "{SENTENCE}"' in constants
    # 预测池那句免责声明是拼出来的，不是把上面那句再抄一次。
    assert 'PREDICTION_DISCLAIMER = f"{SIMILARITY_DISCLAIMER}' in constants


def test_the_similarity_sentence_is_written_once_in_the_frontend():
    """前端同理：只有 `utils/disclaimer.ts` 留字面量，组件和 mock 都 import 它。"""
    hits = _files_saying(ROOT / "frontend" / "src", SENTENCE)
    assert [ROOT / "frontend" / "src" / FRONTEND_HOME] == hits, (
        f"这句话在前端被抄到了 {FRONTEND_HOME} 之外：{hits}"
    )


def test_no_process_cache_or_redis_knob_survives():
    """缓存这一套整个删掉了：这一版没有外部缓存，也没有对应的两个环境变量开关。

    留着它们时，把开关拨到「开」只改到那个没人调用的模块，页面读数一个字节都不变——
    比没有这个开关更难查。这里只扫真会读环境的代码（后端与桌面外壳），
    判据要挡住的是「照着残留的那行又把它接回去」。
    """
    banned = ("REDIS_", "CACHE_ENABLED", "cache_enabled", "redis_explicit", "core.cache", "core import cache")
    hits: list[str] = []
    for directory in (ROOT / "backend", ROOT / "desktop"):
        for path in _sources(directory):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                if token in text:
                    hits.append(f"{path.relative_to(ROOT)}: {token}")
    assert not hits, f"缓存开关又冒出来了：{hits}"
    assert not (ROOT / "backend" / "app" / "core" / "cache.py").exists()


def test_the_dead_helpers_stay_deleted():
    """删掉的兜底函数不许只删调用方、留着定义。

    `get_db` 是 FastAPI 依赖写法，本工程的接口全用 `session_scope()`；
    `listed_too_short` 的「上市满 20 天」由 `similarity_service` 用
    `MIN_LISTED_TRADING_DAYS` 现算，那份是重复实现。
    """
    banned = {
        "backend/app/core/database.py": ("def get_db(",),
        "backend/app/services/limit_up_service.py": ("def listed_too_short(", "REGISTRATION_BOARDS"),
        "backend/app/services/score_model.py": ("FEATURE_COLUMNS",),
        "backend/app/algorithms/vector_index.py": ("def pairwise(", "available_backends"),
        "backend/app/services/ai_analysis_service.py": ("STATUS_IDLE",),
    }
    for relative, tokens in banned.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for token in tokens:
            assert token not in text, f"{relative} 里还留着 {token}"
