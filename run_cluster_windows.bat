@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"

set "PY="
where py >nul 2>nul && py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY where py >nul 2>nul && py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if not defined PY where py >nul 2>nul && py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
if not defined PY where python >nul 2>nul && python -c "import sys; raise SystemExit(0 if (3,11)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo [错误] 没有找到 Python 3.11 ~ 3.13。请到 https://www.python.org/downloads/windows/ 安装 Python 3.12 64 位，勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto :fail
)
call ".venv\Scripts\activate.bat" || goto :fail
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt -r desktop\requirements.txt -r requirements-cluster.txt || goto :fail

if exist "cluster-data\features.json" (
  echo == 对样例特征数据跑离线聚类 ==
  python -m backend.app.tools.cluster --input cluster-data\features.json --k auto --out cluster-data\cluster-result.json || goto :fail
  echo 结果：%CD%\cluster-data\cluster-result.json
)

if not exist "frontend\dist\index.html" (
  where npm >nul 2>nul || (echo [错误] 缺少 frontend\dist，且没有 Node.js。& goto :fail)
  pushd frontend && call npm ci && call npm run build && popd
)
set "LP_STATIC_DIR=%CD%\frontend\dist"
echo == 从源码启动程序，左侧「聚类分析」页可直接运行 ==
python -m desktop
exit /b %errorlevel%

:fail
echo [失败] 请把上面的报错截图发给我。
pause
exit /b 1
