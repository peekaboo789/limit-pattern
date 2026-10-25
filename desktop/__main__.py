"""允许 ``python -m desktop`` 启动桌面外壳。"""

from __future__ import annotations

import multiprocessing

from desktop.launcher import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
