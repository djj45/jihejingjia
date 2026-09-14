#!/usr/bin/env bash
# 一键启动：自动建虚拟环境、装依赖、运行（macOS / Linux）
set -e
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then BIN=python3
  elif command -v python >/dev/null 2>&1; then BIN=python
  else echo "未找到 Python，请先安装 3.10+: https://www.python.org/downloads/"; exit 1
  fi
  "$BIN" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' \
    || { echo "需要 Python 3.10+，当前: $("$BIN" --version)"; exit 1; }
  "$BIN" -m venv .venv
fi

"$PY" -c "import eltdx, fastapi, uvicorn" 2>/dev/null || {
  echo "[jihejingjia] 首次运行，安装依赖 eltdx[http] ..."
  "$PY" -m pip install -q -U "eltdx[http]" \
    || "$PY" -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -U "eltdx[http]"
}

echo "[jihejingjia] 启动中，浏览器打开 http://127.0.0.1:8765"
exec "$PY" app.py
