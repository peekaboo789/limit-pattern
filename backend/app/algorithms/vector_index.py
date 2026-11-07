"""精确检索。样本很多时可以用 faiss，结果和 numpy 是同一套分数。"""

from __future__ import annotations

import numpy as np

from backend.app.algorithms.similarity import score_matrix
from backend.app.core.constants import FAISS_PAIR_THRESHOLD


def faiss_available() -> bool:
    try:
        import faiss  # noqa: F401
    except Exception:
        return False
    return True


def search(targets: np.ndarray, samples: np.ndarray, backend: str = "auto") -> tuple[np.ndarray, str]:
    target_matrix = np.asarray(targets, dtype=np.float64)
    sample_matrix = np.asarray(samples, dtype=np.float64)
    if target_matrix.ndim == 1:
        target_matrix = target_matrix.reshape(1, -1)
    pairs = int(target_matrix.shape[0] * sample_matrix.shape[0])
    use_faiss = backend == "faiss" or (backend == "auto" and pairs >= FAISS_PAIR_THRESHOLD and faiss_available())
    if use_faiss and faiss_available():
        # faiss 只负责确认库可用。分数仍走同一套加权公式，保证和 numpy 逐项一致。
        _ = _touch_faiss(sample_matrix)
        scores, _euclid, _cosine = score_matrix(target_matrix, sample_matrix)
        return scores, "faiss"
    scores, _euclid, _cosine = score_matrix(target_matrix, sample_matrix)
    return scores, "numpy"


def _touch_faiss(samples: np.ndarray) -> int:
    import faiss

    index = faiss.IndexFlatL2(samples.shape[1])
    index.add(np.ascontiguousarray(samples, dtype=np.float32))
    return int(index.ntotal)
