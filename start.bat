@echo off
setlocal
cd /d "%~dp0"
title jihejingjia

set "PY=%~dp0.venv\Scripts\python.exe"

if exist "%PY%" goto deps

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist "%PY%" goto nopy

:deps
"%PY%" -c "import eltdx, fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo [jihejingjia] first run, installing eltdx[http] ...
    "%PY%" -m pip install -q -U "eltdx[http]"
    if errorlevel 1 goto pipfail
)

echo [jihejingjia] starting, open http://127.0.0.1:8765 in your browser
"%PY%" app.py
goto eof

:nopy
echo Python not found. Install Python 3.10+ first: https://www.python.org/downloads/
pause
exit /b 1

:pipfail
echo pip install failed. Check network, or try the tsinghua mirror:
echo   .venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -U "eltdx[http]"
pause
exit /b 1
