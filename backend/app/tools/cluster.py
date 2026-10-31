"""离线聚类命令行。

    python -m backend.app.tools.cluster --input features.json --k auto --out result.json
    python -m backend.app.tools.cluster --input features.csv --k 4 --method agglomerative

输入是「导出特征数据」得到的 JSON（limit-pattern-features/1）或 CSV（code,name,limit_date,特征...）。
需要 scikit-learn：pip install -r requirements-cluster.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三日涨停形态聚类（离线）")
    parser.add_argument("--input", required=True, help="features.json 或 features.csv")
    parser.add_argument("--k", default="auto", help="簇数，或 auto（2~6 里挑轮廓系数最高的）")
    parser.add_argument("--method", default="kmeans", choices=["kmeans", "agglomerative"])
    parser.add_argument("--out", default="", help="结果 JSON；不填就打印")
    args = parser.parse_args(argv)

    from backend.app.services.cluster_service import ClusterUnavailable, dataset_from_csv, run_on_dataset

    path = Path(args.input)
    text = path.read_text(encoding="utf-8-sig")
    dataset = dataset_from_csv(text, path.stem) if path.suffix.lower() == ".csv" else json.loads(text)
    k = None if str(args.k).lower() == "auto" else int(args.k)
    try:
        result = run_on_dataset(dataset, k, args.method)
    except ClusterUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"样本 {result['sample_count']}，分成 {result.get('k', 0)} 组，结果写到 {args.out}")
        for cluster in result["clusters"]:
            print(f"  组{cluster['id'] + 1} {cluster['name']}：{cluster['size']} 只")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
