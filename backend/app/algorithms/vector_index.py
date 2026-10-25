"""精确向量检索：同一批数据、两种后端，分数必须逐项一致。

- `faiss`：`IndexFlatL2`，底层走 BLAS，样本多、目标多的时候比 numpy 快；
- `numpy`：`|a|²+|b|²−2ab` 的恒等式展开，任何环境都能跑，是 faiss 不在时的兜底。

两条路都是**精确检索**——不近似、不截断、不取 top-k 后丢弃其余，所以切换后端只改耗时、
不改读数。`resolve_backend("auto")` 在「目标数 × 样本数」不到 `FAISS_PAIR_THRESHOLD` 时
选 numpy：那个量级上建索引与 float32 转换的开销大于收益。这一条由
`tests/test_clustering_and_vector_index.py` 用「同一批数据、两种后端、逐元素比对」钉住，
比对用的是实测出来的容差，不是拍脑袋的小数。

后端不在设置里、也不在查询参数里：它不是口径，没人需要调它。要看的只有
「这一趟走的是哪条路」，那是 `backend` 属性，不是开关。
"""

from __future__ import annotations

from functools import cache

import numpy as np

from backend.app.core.constants import FAISS_PAIR_THRESHOLD

BACKEND_FAISS = "faiss"
BACKEND_NUMPY = "numpy"
BACKEND_AUTO = "auto"


@cache
def faiss_available() -> bool:
    """装没装 faiss。导入失败按「没有」处理，启动路径上绝不因为可选加速器而崩。"""
    try:
        import faiss  # noqa: F401
    except Exception:
        return False
    return True


def resolve_backend(
    preferred: str = BACKEND_AUTO,
    *,
    n_queries: int = 0,
    n_samples: int = 0,
) -> str:
    """把 `auto` 解析成这一趟真正会走的后端。

    显式要 faiss 而环境里没有时直接报错，不静默降级：静默换路会让「我明明指定了 faiss」
    这种排查变成猜谜。只有 `auto` 才允许因为规模而退回 numpy。
    """
    if preferred == BACKEND_NUMPY:
        return BACKEND_NUMPY
    if preferred == BACKEND_FAISS and not faiss_available():
        raise ValueError("未安装 faiss，无法使用 faiss 后端；请安装 faiss-cpu 或改用 numpy")
    if preferred == BACKEND_FAISS:
        return BACKEND_FAISS
    if preferred != BACKEND_AUTO:
        raise ValueError(f"未知的向量检索后端：{preferred}")
    if not faiss_available():
        return BACKEND_NUMPY
    return BACKEND_FAISS if n_queries * n_samples >= FAISS_PAIR_THRESHOLD else BACKEND_NUMPY


class VectorIndex:
    """对一份「已标准化的样本矩阵」建立的精确检索索引（建好之后不可变）。

    调用方负责把样本放到与打分相同的空间里（RobustScaler + 权重开方）。
    索引不理解向量含义，它只保证一件事：给一批查询，返回每一对之间的欧氏距离。
    """

    def __init__(self, matrix: np.ndarray, backend: str) -> None:
        values = np.ascontiguousarray(np.asarray(matrix, dtype=np.float64))
        if values.ndim != 2:
            raise ValueError("索引输入必须是二维数组")
        if values.shape[0] == 0:
            raise ValueError("样本矩阵是空的，建不了索引")
        self._matrix = values
        self._backend = backend
        self._l2 = None
        if backend == BACKEND_FAISS:
            import faiss

            index = faiss.IndexFlatL2(values.shape[1])
            index.add(values.astype(np.float32))
            self._l2 = index

    @classmethod
    def build(
        cls,
        matrix: np.ndarray,
        *,
        backend: str = BACKEND_AUTO,
        n_queries: int = 0,
    ) -> "VectorIndex":
        values = np.asarray(matrix, dtype=np.float64)
        rows = int(values.shape[0]) if values.ndim == 2 else 0
        resolved = resolve_backend(backend, n_queries=n_queries, n_samples=rows)
        return cls(values, resolved)

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def size(self) -> int:
        return int(self._matrix.shape[0])

    @property
    def dimension(self) -> int:
        return int(self._matrix.shape[1])

    def norms_squared(self) -> np.ndarray:
        """每一行的平方和。numpy 后端靠它与查询的平方和拼出距离。"""
        return np.sum(self._matrix * self._matrix, axis=1)

    def distances(self, queries: np.ndarray) -> np.ndarray:
        """`queries × 全部样本` 的欧氏距离，形状 (查询数, 样本数)。"""
        values = np.ascontiguousarray(np.asarray(queries, dtype=np.float64))
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.shape[1] != self.dimension:
            raise ValueError(f"查询维度 {values.shape[1]} 与索引维度 {self.dimension} 不一致")
        if self._backend == BACKEND_FAISS:
            # faiss 回的是**按距离升序排好**的结果，`indices` 才是样本原来的位置。
            # 直接拿矩阵当距离用会静默把列打乱——分数还是那些数，只是配错了股票。
            squared, indices = self._l2.search(values.astype(np.float32), self.size)
            out = np.empty((values.shape[0], self.size), dtype=np.float64)
            rows = np.arange(values.shape[0])[:, None]
            out[rows, indices] = np.sqrt(np.maximum(squared, 0.0))
            return out
        # 恒等式展开：一次 matmul 出全部交叉项，不展开 (查询, 样本, 维度) 的三维数组。
        # 那一份数组在 5000 × 500 × 67 的量级上是 1.7 GB，扫一轮全市场先把内存吃掉。
        query_sq = np.sum(values * values, axis=1)[:, None]
        cross = values @ self._matrix.T
        return np.sqrt(np.maximum(query_sq + self.norms_squared()[None, :] - 2.0 * cross, 0.0))
