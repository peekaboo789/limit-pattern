@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem 默认打聚类版（带 scikit-learn）。要标准版：先 set LP_WITH_CLUSTER=0
if not defined LP_WITH_CLUSTER set "LP_WITH_CLUSTER=1"

echo == 检查 Python ==
set "PY="
where py >nul 2>nul && py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY where py >nul 2>nul && py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if not defined PY where py >nul 2>nul && py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
if not defined PY where python >nul 2>nul && python -c "import sys; raise SystemExit(0 if (3,11)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo [错误] 没有找到 Python 3.11 ~ 3.13（推荐 3.12，和 CI 一致）。
  echo 请到 https://www.python.org/downloads/windows/ 下载 Python 3.12 64 位安装包，
  echo 安装时勾选 "Add python.exe to PATH"，装好后重新双击本脚本。
  pause
  exit /b 1
)
%PY% --version

echo == 创建虚拟环境 .venv ==
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto :fail
)
call ".venv\Scripts\activate.bat" || goto :fail

if exist "frontend\dist\index.html" (
  set "LP_USE_PREBUILT_FRONTEND=1"
  echo 已有构建好的前端 frontend\dist，不需要 Node.js
)

echo == 按 CI 的 scripts\build_windows.ps1 打包（依赖、图标、测试、PyInstaller、冒烟、zip）==
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\build_windows.ps1" || goto :fail

echo == 复制到 dist\LimitPattern ==
if exist "dist\LimitPattern" rmdir /s /q "dist\LimitPattern"
if not exist "dist" mkdir "dist"
xcopy /e /i /q /y "desktop\dist\LimitPattern" "dist\LimitPattern" >nul || goto :fail
if "%LP_WITH_CLUSTER%"=="1" (set "ZIPNAME=LimitPattern-cluster-win64.zip") else (set "ZIPNAME=LimitPattern-win64.zip")
copy /y "desktop\dist\%ZIPNAME%" "dist\%ZIPNAME%" >nul

echo.
echo ==========================================================
echo 打包完成！
echo 程序：%CD%\dist\LimitPattern\LimitPattern.exe
echo 压缩包：%CD%\dist\%ZIPNAME%
echo ==========================================================
pause
exit /b 0

:fail
echo.
echo [失败] 打包没有完成，请把上面的报错截图发给我。
pause
exit /b 1
