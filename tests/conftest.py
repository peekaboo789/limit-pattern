import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="lp-pytest-"))
os.environ["LP_DATA_DIR"] = str(_ROOT)
os.environ["LP_SCHEDULER"] = "0"
os.environ["LP_AUTO_SYNC"] = "0"
os.environ["LP_STATIC_DIR"] = str(_ROOT / "missing-static")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LP_SCHEDULER", "0")
    monkeypatch.setenv("LP_AUTO_SYNC", "0")
    monkeypatch.setenv("LP_STATIC_DIR", str(tmp_path / "missing-static"))
    from backend.app.core.database import init_db, reset_engine
    from backend.app.services import backtest_service
    from backend.app.services.sync_service import reset_state, set_fetcher

    reset_engine()
    set_fetcher()
    reset_state()
    #: 回测的后台状态是进程级全局量：上一个用例留下的 run_id 会让「库里没行情」那条判据
    #: 读到别人的进度，而不是它自己的起点。
    backtest_service.reset_state()
    init_db()
    from backend.app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    reset_engine()
    set_fetcher()
    backtest_service.reset_state()
