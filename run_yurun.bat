@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo 未找到 Python，请先安装 Python 3.10+ 并勾选 "Add to PATH"。
  pause
  exit /b 1
)
echo 语润 Yurun 启动中...（按住 ` 键说话，关闭此窗口退出）
%PY% src/main.py
echo [done] python 已退出。若有红色报错，请截图。
pause
