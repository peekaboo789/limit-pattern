"""导出某天的聚类特征数据。

    python -m backend.app.tools.export_features --date 2026-10-30 --out features.json
    python -m backend.app.tools.export_features --out features.csv   （默认最新交易日）
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出聚类特征数据（JSON / CSV）")
    parser.add_argument("--date", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    from backend.app.core.database import init_db, session_scope
    from backend.app.services.cluster_service import build_dataset, dataset_to_csv
    from backend.app.services.retention import quote_dates

    init_db()
    with session_scope() as session:
        days = quote_dates(session)
        if not days:
            print("库里还没有行情")
            return 2
        day = date.fromisoformat(args.date) if args.date else days[-1]
        dataset = build_dataset(session, day)
    out = Path(args.out)
    if out.suffix.lower() == ".csv":
        out.write_text(dataset_to_csv(dataset), encoding="utf-8-sig")
    else:
        out.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{day} 样本 {len(dataset['samples'])} 条，写到 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
