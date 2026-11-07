"""打 Docker 程序内更新包：limit-pattern-app-<ver>.tar.gz（backend + 已构建的 frontend/dist + requirements + manifest.json）和 .sha256。

python scripts/build_app_package.py [输出目录] [--version 覆盖版本号（只用于测试）]
需要先 cd frontend && npm run build。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import re
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("lp_supervisor", ROOT / "docker" / "supervisor.py")
sup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sup)


def _skip(path: Path) -> bool:
    parts = set(path.parts)
    return "__pycache__" in parts or "tests" in parts or path.suffix in (".pyc", ".db", ".log") or "data" in path.relative_to(ROOT).parts[1:2]


def build(out: Path, version: str | None = None) -> Path:
    real = sup.code_version(ROOT)
    version = version or real
    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("先构建前端：cd frontend && npm run build")
    out.mkdir(parents=True, exist_ok=True)
    pkg = out / f"limit-pattern-app-{version}.tar.gz"
    manifest = {"format": "limit-pattern-app", "version": version, "requirements_sha256": sup.requirements_hash(ROOT),
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    def add_bytes(tf, name, data: bytes):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = int(time.time())
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(data))

    with tarfile.open(pkg, "w:gz") as tf:
        for base in (ROOT / "backend", dist):
            for p in sorted(base.rglob("*")):
                if p.is_file() and not _skip(p):
                    rel = p.relative_to(ROOT).as_posix()
                    data = p.read_bytes()
                    if rel == "backend/app/__init__.py" and version != real:
                        data = re.sub(rb'__version__ = ".+?"', f'__version__ = "{version}"'.encode(), data)
                    add_bytes(tf, rel, data)
        for name in ("requirements.txt", "requirements-cluster.txt"):
            add_bytes(tf, name, (ROOT / name).read_bytes())
        add_bytes(tf, "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode())
    digest = hashlib.sha256(pkg.read_bytes()).hexdigest()
    (out / (pkg.name + ".sha256")).write_text(f"{digest}  {pkg.name}\n", encoding="utf-8")
    return pkg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="dist")
    ap.add_argument("--version")
    a = ap.parse_args()
    print(build(Path(a.out), a.version))
