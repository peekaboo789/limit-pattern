"""桌面启动器必须跑真实后端，打包清单要带上行情源和定时任务。"""

from __future__ import annotations

import importlib.util

import pytest

from desktop.launcher import main


def test_smoke_uses_the_real_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LP_AUTO_SYNC", "0")
    monkeypatch.setenv("LP_SCHEDULER", "0")
    monkeypatch.setenv("LP_STATIC_DIR", str(tmp_path / "missing-static"))
    monkeypatch.delenv("LP_USE_STUB", raising=False)
    assert main(["--smoke", "--port", "18765"]) == 0
    text = (tmp_path / "logs" / "launcher.log").read_text(encoding="utf-8")
    assert "应用来源=backend" in text
    assert "-stub" not in text
    assert "健康检查通过" in text


@pytest.mark.skipif(importlib.util.find_spec("PyInstaller") is None, reason="未安装 PyInstaller")
def test_hiddenimports_cover_backend_providers_and_scheduler():
    from desktop.pyi_bundle import REQUIRED_BACKEND, analysis_kwargs

    built = analysis_kwargs()
    hidden = set(built["hiddenimports"])
    missing = [name for name in REQUIRED_BACKEND if name not in hidden]
    assert missing == []
    for name in ("numpy", "httpx", "apscheduler.schedulers.background", "sqlalchemy.dialects.sqlite"):
        assert name in hidden, name
    assert "sklearn" in built["excludes"]
